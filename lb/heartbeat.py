"""UDP listener that receives CPU/memory/connection heartbeats from backend agents.

Why UDP here but TCP for requests: a heartbeat is a "latest value" report
sent every second. If one is lost the next one replaces it a second later,
so TCP's retransmission, ordering and per-connection state would only add
cost (and a stuck TCP connection would delay newer data behind older data).
A proxied HTTP request is the opposite: every byte must arrive, exactly once,
in order, so it needs TCP.

UDP gives no ordering or duplicate protection, so each datagram carries the
agent's start time and a sequence number; anything not newer than the last
accepted one from that backend is dropped. (A restarted agent has a newer
start time, so its seq can begin again from 0.)

Security note: UDP datagrams are trivially spoofed and these are not
authenticated, so the listener binds to loopback by default and only accepts
reports for backends the router already knows. A real deployment would sign
them (e.g. an HMAC with a shared key).

Datagram (JSON):
  {"backend": "127.0.0.1:9001", "started_at": 1761100000.0, "seq": 42,
   "cpu_percent": 12.5, "memory_percent": 0.4, "active_connections": 3}
"""

import json
import logging
import math
import socket
import threading
import time
from typing import TYPE_CHECKING

from lb.router import Router
from lb.strategies.base import Backend

if TYPE_CHECKING:
    from lb.db_writer import DBWriter

log = logging.getLogger(__name__)

MAX_DATAGRAM = 2048  # a heartbeat is ~150 bytes; anything bigger is not ours
POLL_SECONDS = 0.5   # recvfrom timeout, so the thread notices stop()


def parse_heartbeat(data: bytes) -> tuple[Backend, float, int, float, float, int]:
    """Validate one datagram. Returns (backend, started_at, seq, cpu, memory, conns); raises ValueError."""
    try:
        msg = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("not JSON") from None
    if not isinstance(msg, dict):
        raise ValueError("not a JSON object")
    backend = Backend.parse(str(msg.get("backend", "")))
    started_at, cpu, memory = (msg.get(k) for k in ("started_at", "cpu_percent", "memory_percent"))
    seq, conns = msg.get("seq"), msg.get("active_connections")
    numbers = (started_at, cpu, memory)
    # bool is a subclass of int in Python; reject it explicitly.
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in numbers):
        raise ValueError("started_at, cpu_percent and memory_percent must be finite numbers")
    if not all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (seq, conns)):
        raise ValueError("seq and active_connections must be non-negative integers")
    if not (0 <= cpu <= 100 and 0 <= memory <= 100):
        raise ValueError("percentages must be between 0 and 100")
    return backend, float(started_at), seq, float(cpu), float(memory), conns


class HeartbeatListener:
    def __init__(self, router: Router, host: str = "127.0.0.1", port: int = 9100,
                 db: "DBWriter | None" = None):
        self.router = router
        self.db = db  # optional: persist every accepted heartbeat into server_metrics (Phase 4)
        self._address = (host, port)
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat-listener", daemon=True)
        # Only this thread touches these, so they need no lock.
        self._last_seen: dict[Backend, tuple[float, int]] = {}
        self.accepted = 0
        self.dropped = 0

    def start(self) -> tuple[str, int]:
        """Bind the UDP socket and start the thread. Returns the bound address (port 0 picks one)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # IPv4 + UDP
        sock.bind(self._address)
        # No listen()/accept(): UDP is connectionless. Each recvfrom() returns
        # one whole datagram (message boundaries are kept, unlike TCP).
        sock.settimeout(POLL_SECONDS)
        self._sock = sock
        self._thread.start()
        return sock.getsockname()[:2]

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2 * POLL_SECONDS + 1)
        if self._sock is not None:
            self._sock.close()

    def handle(self, data: bytes, received_at: float) -> bool:
        """Process one datagram. Returns True if it updated the stats."""
        try:
            backend, started_at, seq, cpu, memory, conns = parse_heartbeat(data)
        except ValueError as exc:
            log.debug("dropping malformed heartbeat: %s", exc)
            self.dropped += 1
            return False
        if backend not in self.router.backends:
            self.dropped += 1  # unknown sender: don't let stray datagrams create stats entries
            return False
        if (started_at, seq) <= self._last_seen.get(backend, (-math.inf, -1)):
            self.dropped += 1  # duplicate or arrived out of order: we already have newer data
            return False
        self._last_seen[backend] = (started_at, seq)
        self.router.stats.record_heartbeat(backend, cpu, memory, conns, received_at)
        if self.db:
            self.db.log_metric(backend, cpu, memory, conns)
        self.accepted += 1
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(MAX_DATAGRAM)
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return
                log.warning("heartbeat recvfrom failed: %s", exc)
                continue
            self.handle(data, time.monotonic())
