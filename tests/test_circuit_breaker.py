"""Every circuit breaker transition, driven by a fake clock (no sleeping)."""

import threading

import pytest

from lb.circuit_breaker import CircuitBreaker
from lb.config import BreakerSettings
from lb.router import NoBackendAvailable, Router
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_round_robin import REQUEST

SETTINGS = BreakerSettings(failure_threshold=3, cooldown=10.0, half_open_trials=2)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def breaker(clock):
    return CircuitBreaker(SETTINGS, clock)


def trip(breaker):
    for _ in range(SETTINGS.failure_threshold):
        assert breaker.try_acquire()
        breaker.record_failure()


def test_starts_closed_and_lets_requests_through(breaker):
    assert breaker.state == "closed"
    assert breaker.can_attempt() and breaker.try_acquire()


def test_closed_to_open_after_threshold_consecutive_failures(breaker):
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open"


def test_a_success_resets_the_consecutive_failure_count(breaker):
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"  # never 3 in a row


def test_open_rejects_requests_until_the_cooldown_passes(breaker, clock):
    trip(breaker)
    clock.now += SETTINGS.cooldown - 0.1
    assert not breaker.can_attempt()
    assert not breaker.try_acquire()
    assert breaker.state == "open"


def test_open_to_half_open_after_cooldown(breaker, clock):
    trip(breaker)
    clock.now += SETTINGS.cooldown
    assert breaker.can_attempt()
    assert breaker.state == "open"  # can_attempt() has no side effects
    assert breaker.try_acquire()     # the first request after the cooldown flips it
    assert breaker.state == "half_open"


def test_half_open_allows_only_the_trial_requests(breaker, clock):
    trip(breaker)
    clock.now += SETTINGS.cooldown
    assert breaker.try_acquire() and breaker.try_acquire()
    assert not breaker.try_acquire()  # both trials in flight: everyone else waits
    assert not breaker.can_attempt()


def test_half_open_to_closed_when_all_trials_succeed(breaker, clock):
    trip(breaker)
    clock.now += SETTINGS.cooldown
    breaker.try_acquire()
    breaker.try_acquire()
    breaker.record_success()
    assert breaker.state == "half_open"
    breaker.record_success()
    assert breaker.state == "closed"
    # A fresh CLOSED state: the failure count starts from zero again.
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_half_open_to_open_on_a_failed_trial_with_a_new_cooldown(breaker, clock):
    trip(breaker)
    clock.now += SETTINGS.cooldown
    breaker.try_acquire()
    breaker.record_failure()
    assert breaker.state == "open"
    clock.now += SETTINGS.cooldown - 0.1  # the cooldown restarted at the failed trial
    assert not breaker.try_acquire()
    clock.now += 0.1
    assert breaker.try_acquire() and breaker.state == "half_open"


def test_late_results_while_open_are_ignored(breaker):
    trip(breaker)
    breaker.record_success()  # a request that started before the circuit opened
    assert breaker.state == "open"


def test_concurrent_half_open_acquires_never_exceed_the_trial_limit(breaker, clock):
    trip(breaker)
    clock.now += SETTINGS.cooldown
    granted = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        if breaker.try_acquire():
            granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == SETTINGS.half_open_trials


def test_router_skips_a_backend_whose_circuit_is_open():
    a, b = Backend("127.0.0.1", 1), Backend("127.0.0.1", 2)
    router = Router([a, b], RoundRobinStrategy(), BreakerSettings(failure_threshold=1, cooldown=60))
    assert router.acquire(REQUEST) == a
    router.release(a, success=False)  # opens a's circuit
    assert [router.choose(REQUEST) for _ in range(4)] == [b] * 4
    router.release(router.acquire(REQUEST), success=False)  # and b's
    with pytest.raises(NoBackendAvailable):
        router.acquire(REQUEST)
