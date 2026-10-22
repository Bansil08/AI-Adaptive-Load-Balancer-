"""Shared per-backend statistics protected by a readers-writers lock.

Every worker thread updates these (request started / finished) and every
routing decision may read them (least connections in Phase 5, the ML
strategies in Phase 6), so they need synchronisation. See lb/rwlock.py for
why a readers-writers lock and why the writer-preference variant.

Honest caveat: today every request takes the write lock twice and nothing
reads on the hot path yet, so for now a plain mutex would do the same job.
The readers-writers lock pays off once least connections (Phase 5) reads a
snapshot on every routing decision and the dashboard polls it.
"""

from dataclasses import dataclass
from typing import Iterable

from lb.rwlock import ReadWriteLock
from lb.strategies.base import Backend

# Weight of the newest sample in the recent-latency average (see _Counters).
LATENCY_EWMA_ALPHA = 0.2


@dataclass(frozen=True)
class BackendStats:
    """An immutable copy handed to readers, so they can use it after the lock is released."""

    active_connections: int
    total_requests: int
    avg_latency_ms: float | None  # None until the first request has finished
    # Latest UDP heartbeat from the backend's agent (lb/heartbeat.py); None until one arrives.
    cpu_percent: float | None = None
    memory_percent: float | None = None
    reported_connections: int | None = None  # in-flight requests as the backend counts them
    heartbeat_at: float | None = None        # time.monotonic() when it was received


class _Counters:
    """Mutable per-backend values. Only touched while holding the stats lock."""

    __slots__ = ("active", "total", "avg_latency_ms", "cpu", "memory", "reported", "heartbeat_at")

    def __init__(self) -> None:
        self.active = 0
        self.total = 0
        self.avg_latency_ms: float | None = None
        self.cpu: float | None = None
        self.memory: float | None = None
        self.reported: int | None = None
        self.heartbeat_at: float | None = None


class ServerStats:
    def __init__(self, backends: Iterable[Backend] = ()):
        self._lock = ReadWriteLock()
        self._counters: dict[Backend, _Counters] = {b: _Counters() for b in backends}

    def request_started(self, backend: Backend) -> None:
        with self._lock.write_locked():
            # setdefault: a backend added at runtime (Phase 3 admin) just appears.
            counters = self._counters.setdefault(backend, _Counters())
            counters.active += 1
            counters.total += 1

    def request_finished(self, backend: Backend, latency_ms: float | None) -> None:
        """latency_ms=None: the request failed without telling us the backend's speed."""
        with self._lock.write_locked():
            counters = self._counters.setdefault(backend, _Counters())
            counters.active -= 1
            if latency_ms is None:
                return
            # Exponentially weighted moving average: new = a*sample + (1-a)*old.
            # O(1) memory and recent requests count most, so it follows a backend
            # that slows down; with a=0.2, a sample's weight halves every ~3 requests.
            if counters.avg_latency_ms is None:
                counters.avg_latency_ms = latency_ms
            else:
                counters.avg_latency_ms = (
                    LATENCY_EWMA_ALPHA * latency_ms + (1 - LATENCY_EWMA_ALPHA) * counters.avg_latency_ms
                )

    def record_heartbeat(self, backend: Backend, cpu_percent: float, memory_percent: float,
                         active_connections: int, received_at: float) -> None:
        with self._lock.write_locked():
            counters = self._counters.setdefault(backend, _Counters())
            counters.cpu = cpu_percent
            counters.memory = memory_percent
            counters.reported = active_connections
            counters.heartbeat_at = received_at

    def get(self, backend: Backend) -> BackendStats:
        with self._lock.read_locked():
            return self._freeze(self._counters.get(backend, _Counters()))

    def snapshot(self) -> dict[Backend, BackendStats]:
        """All backends at one consistent instant (no update can land halfway through)."""
        with self._lock.read_locked():
            return {backend: self._freeze(c) for backend, c in self._counters.items()}

    @staticmethod
    def _freeze(c: _Counters) -> BackendStats:
        return BackendStats(c.active, c.total, c.avg_latency_ms, c.cpu, c.memory, c.reported, c.heartbeat_at)
