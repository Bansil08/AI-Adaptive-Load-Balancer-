"""Agent that sends CPU, memory and active-connection heartbeats over UDP.

Runs as a thread inside the backend process (backend/app.py --heartbeat),
because the in-flight request count lives in that process's memory.

CPU and memory are for THIS process, not the whole machine: in development
all backends run on one machine, so system-wide numbers would be identical
for every backend and tell the load balancer nothing. psutil's process CPU %
is relative to one core (it can exceed 100 on a multi-core machine), so it is
divided by the core count to give a 0-100 share of the machine.

See lb/heartbeat.py for the datagram format and why this is UDP.
"""

import json
import socket
import threading
import time
from typing import Callable

import psutil


class HeartbeatAgent:
    def __init__(
        self,
        lb_address: tuple[str, int],
        advertise: str,                      # "host:port" the LB knows this backend by
        active_connections: Callable[[], int],
        interval: float = 1.0,
    ):
        self.lb_address = lb_address
        self.advertise = advertise
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

    def build(self) -> bytes:
        self.seq += 1
        cpu = min(100.0, self._process.cpu_percent(None) / self._cpu_count)
        return json.dumps({
            "backend": self.advertise,
            "started_at": self.started_at,
            "seq": self.seq,
            "cpu_percent": round(cpu, 2),
            "memory_percent": round(self._process.memory_percent(), 2),
            "active_connections": self.active_connections(),
        }).encode()

    def _run(self) -> None:
        # One unconnected UDP socket: sendto() just hands a datagram to the
        # kernel. No handshake, and nothing blocks if the LB isn't running
        # (the datagram is simply lost, which is fine for heartbeats).
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while not self._stop.wait(self.interval):
                try:
                    sock.sendto(self.build(), self.lb_address)
                except OSError:
                    # e.g. ECONNREFUSED reported from an earlier ICMP "port
                    # unreachable" while the LB is down. Keep sending.
                    pass
