"""Tests for the round-robin strategy and the router."""

import threading
from collections import Counter

import pytest

from lb.router import NoBackendAvailable, Router
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy

BACKENDS = [Backend("127.0.0.1", 9001), Backend("127.0.0.1", 9002), Backend("127.0.0.1", 9003)]


def test_cycles_through_backends_in_order():
    rr = RoundRobinStrategy()
    picks = [rr.choose(BACKENDS).port for _ in range(7)]
    assert picks == [9001, 9002, 9003, 9001, 9002, 9003, 9001]


def test_handles_backend_list_shrinking():
    rr = RoundRobinStrategy()
    rr.choose(BACKENDS)
    rr.choose(BACKENDS)
    # The counter is taken modulo the current length, so it never indexes out of range.
    assert rr.choose(BACKENDS[:1]).port == 9001


def test_concurrent_choices_are_perfectly_balanced():
    """With the lock, 8 threads x 3000 picks split exactly evenly (no lost updates)."""
    rr = RoundRobinStrategy()
    counts = Counter()
    counts_lock = threading.Lock()

    def worker():
        local = Counter(rr.choose(BACKENDS).port for _ in range(3000))
        with counts_lock:
            counts.update(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counts == {9001: 8000, 9002: 8000, 9003: 8000}


def test_router_without_backends_raises():
    with pytest.raises(NoBackendAvailable):
        Router([], RoundRobinStrategy()).choose()


@pytest.mark.parametrize("text", ["localhost", ":80", "host:", "host:abc", "host:٣", "host:0", "host:70000", "[]:80"])
def test_backend_parse_rejects_bad_input(text):
    with pytest.raises(ValueError):
        Backend.parse(text)


def test_backend_parse():
    assert Backend.parse(" 10.0.0.1:9001 ") == Backend("10.0.0.1", 9001)


def test_backend_parse_ipv6_strips_brackets():
    backend = Backend.parse("[::1]:9001")
    assert backend == Backend("::1", 9001)
    assert str(backend) == "[::1]:9001"
