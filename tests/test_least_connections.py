"""Least connections: picks the least-busy backend from the live stats, rotates ties, stays thread-safe."""

import threading
from collections import Counter

import pytest

from lb.router import Router
from lb.strategies.base import Backend
from lb.strategies.least_connections import LeastConnectionsStrategy
from tests.test_round_robin import REQUEST

A, B, C = (Backend("127.0.0.1", p) for p in (9001, 9002, 9003))


@pytest.fixture
def router():
    return Router([A, B, C], LeastConnectionsStrategy())


def busy(router, backend, n):
    for _ in range(n):
        router.stats.request_started(backend)


def test_picks_the_backend_with_fewest_active_connections(router):
    busy(router, A, 3)
    busy(router, B, 1)
    busy(router, C, 2)
    assert router.choose(REQUEST) == B


def test_follows_the_stats_as_requests_finish(router):
    busy(router, A, 2)
    busy(router, B, 2)
    busy(router, C, 2)
    router.stats.request_finished(C, 10.0)
    assert router.choose(REQUEST) == C


def test_ties_are_rotated_not_always_the_first(router):
    # All idle: without rotation every pick would be A.
    assert [router.choose(REQUEST) for _ in range(6)] == [A, B, C, A, B, C]


def test_ties_rotate_among_the_tied_backends_only(router):
    busy(router, B, 5)
    assert Counter(router.choose(REQUEST) for _ in range(10)) == {A: 5, C: 5}


def test_only_candidates_are_considered(router):
    busy(router, A, 0)
    busy(router, B, 4)
    router.set_healthy(A, False)  # the router passes only B and C to the strategy
    assert router.choose(REQUEST) == C


def test_slow_backend_ends_up_with_less_traffic():
    """Simulated: each request holds its backend for `cost` ticks. Least
    connections should send far fewer requests to the slow backend."""
    router = Router([A, B], LeastConnectionsStrategy())
    cost = {A: 1, B: 5}  # B is five times slower
    in_flight = []        # (finish tick, backend)
    sent = Counter()
    for tick in range(1000):
        for item in [i for i in in_flight if i[0] <= tick]:
            in_flight.remove(item)
            router.stats.request_finished(item[1], 1.0)
        for _ in range(2):  # two new requests per tick
            backend = router.choose(REQUEST)
            router.stats.request_started(backend)
            in_flight.append((tick + cost[backend], backend))
            sent[backend] += 1
    assert sent[A] > 3 * sent[B]


def test_needs_stats():
    with pytest.raises(RuntimeError):
        LeastConnectionsStrategy().choose([A], REQUEST)


def test_concurrent_choices_are_safe_and_spread_ties(router):
    picks = Counter()
    lock = threading.Lock()

    def worker():
        local = Counter(router.choose(REQUEST) for _ in range(3000))
        with lock:
            picks.update(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Everything idle the whole time: the tie rotation splits exactly evenly.
    assert picks == {A: 8000, B: 8000, C: 8000}


def test_end_to_end_prefers_the_fast_backend():
    import json
    from lb.proxy import Proxy
    from lb.server import LoadBalancer
    from tests.test_proxy import as_backend, request, start_backend

    fast, slow = start_backend("fast", delay_ms=5), start_backend("slow", delay_ms=200)
    router = Router([as_backend(fast), as_backend(slow)], LeastConnectionsStrategy())
    lb = LoadBalancer(router, "127.0.0.1", 0, proxy=Proxy(router))
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    served = Counter()
    lock = threading.Lock()

    def client():
        for _ in range(10):
            name = json.loads(request(port)[2])["server"]
            with lock:
                served[name] += 1

    try:
        threads = [threading.Thread(target=client) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        lb.shutdown()
        for s in (fast, slow):
            s.shutdown()
            s.server_close()
    # Round robin would split 30/30; least connections keeps the slow one busy with few.
    assert served["fast"] > 2 * served["slow"]
