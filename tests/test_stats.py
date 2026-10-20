"""Tests for lb.stats.ServerStats."""

import sys
import threading

import pytest

from lb.rwlock import ReadWriteLock
from lb.stats import LATENCY_EWMA_ALPHA, BackendStats, ServerStats
from lb.strategies.base import Backend

A = Backend("127.0.0.1", 9001)
B = Backend("127.0.0.1", 9002)


def test_starts_at_zero():
    stats = ServerStats([A, B])
    assert stats.snapshot() == {A: BackendStats(0, 0, None), B: BackendStats(0, 0, None)}


def test_active_and_total_counts():
    stats = ServerStats([A])
    stats.request_started(A)
    stats.request_started(A)
    assert stats.get(A) == BackendStats(active_connections=2, total_requests=2, avg_latency_ms=None)
    stats.request_finished(A, 10)
    assert stats.get(A).active_connections == 1
    assert stats.get(A).total_requests == 2


def test_latency_average_is_exponentially_weighted():
    stats = ServerStats([A])
    for latency in (100, 200):
        stats.request_started(A)
        stats.request_finished(A, latency)
    expected = LATENCY_EWMA_ALPHA * 200 + (1 - LATENCY_EWMA_ALPHA) * 100
    assert stats.get(A).avg_latency_ms == pytest.approx(expected)


def test_average_follows_a_backend_that_slows_down():
    stats = ServerStats([A])
    for latency in [20] * 50 + [500] * 20:
        stats.request_started(A)
        stats.request_finished(A, latency)
    # A plain mean of all 70 samples would be ~157 ms; the EWMA is near 500.
    assert stats.get(A).avg_latency_ms > 490


def test_failure_without_latency_leaves_the_average_alone():
    stats = ServerStats([A])
    stats.request_started(A)
    stats.request_finished(A, 100)
    stats.request_started(A)
    stats.request_finished(A, None)  # e.g. connection refused after ~1 ms
    assert stats.get(A) == BackendStats(active_connections=0, total_requests=2, avg_latency_ms=100)


def test_unknown_backend_is_added_on_first_use():
    stats = ServerStats()
    stats.request_started(A)
    assert stats.snapshot() == {A: BackendStats(1, 1, None)}


def test_snapshot_is_a_copy():
    stats = ServerStats([A])
    before = stats.snapshot()
    stats.request_started(A)
    assert before[A].active_connections == 0


@pytest.fixture
def frequent_thread_switches():
    """By default the GIL switches threads only every 5 ms, so a race inside
    `x += 1` almost never shows up in a short test. Switching every microsecond
    makes the interleaving that breaks unlocked code likely."""
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(old)


def test_concurrent_updates_are_not_lost(frequent_thread_switches):
    """`active += 1` is read-modify-write: two threads can both read 5 and
    both write 6. On standard CPython the GIL only switches threads at calls
    and loop jumps, so this exact race rarely fires here even without the lock
    (it does on the free-threaded build); the lock-usage test below covers that gap."""
    stats = ServerStats([A, B])
    per_thread = 2000

    def work(backend):
        for _ in range(per_thread):
            stats.request_started(backend)
            stats.snapshot()  # readers interleaved with writers
            stats.request_finished(backend, 1.0)

    threads = [threading.Thread(target=work, args=(A if i % 2 else B,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    for backend in (A, B):
        assert stats.get(backend) == BackendStats(0, 4 * per_thread, 1.0)


class SpyLock(ReadWriteLock):
    """A real ReadWriteLock that also records how it is used."""

    def __init__(self):
        super().__init__()
        self.reads = self.writes = 0
        self.holding_write = False

    def acquire_read(self):
        super().acquire_read()
        self.reads += 1

    def acquire_write(self):
        super().acquire_write()
        self.writes += 1
        self.holding_write = True

    def release_write(self):
        self.holding_write = False
        super().release_write()


def test_every_update_holds_the_write_lock_and_every_read_the_read_lock():
    stats = ServerStats([A])
    spy = stats._lock = SpyLock()
    seen_while_writing = []

    class Watched:
        """Counters that note whether the write lock is held whenever they change."""

        def __init__(self):
            object.__setattr__(self, "active", 0)
            object.__setattr__(self, "total", 0)
            object.__setattr__(self, "avg_latency_ms", None)

        def __setattr__(self, name, value):
            seen_while_writing.append(spy.holding_write)
            object.__setattr__(self, name, value)

    stats._counters[A] = Watched()
    stats.request_started(A)
    stats.request_finished(A, 10)
    stats.request_started(A)
    stats.request_finished(A, None)
    stats.get(A)
    stats.snapshot()
    assert seen_while_writing and all(seen_while_writing), "a counter changed without the write lock"
    assert (spy.writes, spy.reads) == (4, 2)
