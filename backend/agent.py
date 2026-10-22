"""Agent that sends CPU, memory and active-connection heartbeats over UDP.

Runs as a thread inside the backend process (backend/app.py --heartbeat),
because the in-flight request count lives in that process's memory.

CPU and memory are for THIS process, not the whole machine: in development
all backends run on one machine, so system-wide numbers would be identical
for every backend and tell the load balancer nothing. psutil's process CPU %
is relative to one core (it can exceed 100 on a multi-core machine), so it is
divided by the core count to give a 0-100 share of the machine.

An agent can report to several load balancers, each knowing the backend by
its own address: in Docker Compose the LB container reaches it as
backend-fast:9001, an LB run on the host as 127.0.0.1:9001.

See lb/heartbeat.py for the datagram format and why this is UDP.
"""

import json
import socket
import threading
import time
from typing import Callable

import psutil


Target = tuple[tuple[str, int], str]  # ((LB host, LB UDP port), "host:port" that LB knows us by)


def parse_target(text: str, default_advertise: str) -> Target:
    """'lb:9100' or 'lb:9100=backend-fast:9001' -> (('lb', 9100), advertised address)."""
    address, _, advertise = text.partition("=")
    host, sep, port = address.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"expected HOST:PORT[=ADVERTISE], got {text!r}")
    return (host, int(port)), advertise or default_advertise


class HeartbeatAgent:
    def __init__(
        self,
        targets: list[Target],
        active_connections: Callable[[], int],
        interval: float = 1.0,
    ):
        self.targets = targets
        self.active_connections = active_connections
        self.interval = interval
        self.started_at = time.time()  # lets the LB tell a restarted agent from a replayed packet
        self.seq = 0
        self._process = psutil.Process()
        self._cpu_count = psutil.cpu_count() or 1
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat-agent", daemon=True)

    def start(self) -> None:
        # The first cpu_percent() call only sets the baseline and returns 0.0;
        # every later call reports usage since the previous call.
        self._process.cpu_percent(None)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(self.interval + 1)

    def measure(self) -> dict:
        """One reading, shared by every target (cpu_percent() resets its baseline when called)."""
        self.seq += 1
        cpu = min(100.0, self._process.cpu_percent(None) / self._cpu_count)
        return {
            "started_at": self.started_at,
            "seq": self.seq,
            "cpu_percent": round(cpu, 2),
            "memory_percent": round(self._process.memory_percent(), 2),
            "active_connections": self.active_connections(),
        }

    @staticmethod
    def build(reading: dict, advertise: str) -> bytes:
        return json.dumps({"backend": advertise, **reading}).encode()

    def _run(self) -> None:
        # One unconnected UDP socket: sendto() just hands a datagram to the
        # kernel. No handshake, and nothing blocks if the LB isn't running
        # (the datagram is simply lost, which is fine for heartbeats).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while not self._stop.wait(self.interval):
                reading = self.measure()
                for address, advertise in self.targets:
                    try:
                        sock.sendto(self.build(reading, advertise), address)
                    except OSError:
                        # e.g. ECONNREFUSED reported from an earlier ICMP "port
                        # unreachable" while that LB is down, or a name that
                        # doesn't resolve (no LB container). Keep sending.
                        pass
