"""Epsilon-greedy bandit: finds the best arm, adapts when it changes, penalises failures, falls back safely."""

import random
from collections import Counter

import pytest

from lb.router import Router
from lb.strategies.bandit import EpsilonGreedyStrategy
from lb.strategies.base import Backend
from tests.test_round_robin import REQUEST

A, B, C = (Backend("127.0.0.1", p) for p in (9001, 9002, 9003))


def make(**kwargs) -> tuple[EpsilonGreedyStrategy, Router]:
    strategy = EpsilonGreedyStrategy(seed=1, **kwargs)
    return strategy, Router([A, B, C], strategy)


def simulate(strategy, router, latency_of, steps, seed=7) -> list[Backend]:
    """Route `steps` requests; each "takes" latency_of[backend] ms +-20% (a fixed simulated reward)."""
    noise = random.Random(seed)
    chosen = []
    for _ in range(steps):
        backend = router.choose(REQUEST)
        strategy.observe(backend, latency_of[backend] * noise.uniform(0.8, 1.2))
        chosen.append(backend)
    return chosen


def test_chooses_the_best_arm_most_of_the_time():
    strategy, router = make(epsilon=0.1)
    chosen = simulate(strategy, router, {A: 50, B: 10, C: 100}, 3000)
    share = Counter(chosen[300:])  # after a short learning period
    # Expected: 90% greedy + 10%/3 exploring = ~93% on B.
    assert share[B] / sum(share.values()) > 0.85
    assert share[A] > 0 and share[C] > 0  # exploration still samples the others


def test_shifts_to_the_new_best_arm_after_a_change():
    strategy, router = make(epsilon=0.1, window=50)
    simulate(strategy, router, {A: 50, B: 10, C: 100}, 2000)
    # B degrades badly; A is now the best.
    after = simulate(strategy, router, {A: 50, B: 200, C: 100}, 2000)
    late = Counter(after[500:])
    assert late[A] / sum(late.values()) > 0.85


def test_without_forgetting_the_shift_would_take_much_longer():
    # Same change with a huge window (almost no forgetting) for contrast: B's long
    # fast history keeps its mean low for a while, so A wins fewer requests.
    def share_of_a(window):
        strategy, router = make(epsilon=0.1, window=window)
        simulate(strategy, router, {A: 50, B: 10, C: 100}, 2000)
        after = simulate(strategy, router, {A: 50, B: 200, C: 100}, 300)
        return Counter(after)[A] / len(after)
    assert share_of_a(50) > share_of_a(100_000) + 0.2


def test_epsilon_zero_is_purely_greedy_and_one_is_purely_random():
    strategy, router = make(epsilon=0.0)
    chosen = simulate(strategy, router, {A: 50, B: 10, C: 100}, 600)
    assert set(chosen[10:]) == {B}
    strategy, router = make(epsilon=1.0)
    share = Counter(simulate(strategy, router, {A: 50, B: 10, C: 100}, 3000))
    assert all(800 < share[b] < 1200 for b in (A, B, C))


def test_untried_arms_are_tried_first():
    strategy, router = make(epsilon=0.0)
    strategy.observe(A, 10.0)
    # B and C have no estimate yet: they are tried (least connections among them).
    assert {router.choose(REQUEST) for _ in range(4)} == {B, C}


def test_a_failure_counts_as_the_penalty_not_as_a_fast_answer():
    strategy, router = make(epsilon=0.0, failure_penalty_ms=1000.0)
    strategy.observe(A, 30.0)
    strategy.observe(B, 40.0)
    strategy.observe(C, None)  # e.g. connection refused after 1 ms
    assert strategy.estimates()[C] == -1000.0
    assert router.choose(REQUEST) == A


def test_the_window_keeps_only_the_latest_rewards():
    strategy, _ = make(window=3)
    for latency in (100, 100, 100, 10, 10, 10):
        strategy.observe(A, latency)
    assert strategy.estimates()[A] == pytest.approx(-10.0)


def test_only_candidates_are_chosen():
    # The router passes only healthy backends; the bandit must not pick a
    # better-looking arm that is not among them.
    strategy, router = make(epsilon=0.5)
    for backend, latency in ((A, 5), (B, 50), (C, 50)):
        strategy.observe(backend, latency)
    router.set_healthy(A, False)
    assert A not in {router.choose(REQUEST) for _ in range(200)}


def test_falls_back_to_least_connections_when_choosing_fails(caplog):
    strategy, router = make()

    def broken(backends, request):
        raise ZeroDivisionError("bug")
    strategy._choose = broken
    router.stats.request_started(A)
    router.stats.request_started(C)
    with caplog.at_level("WARNING"):
        assert router.choose(REQUEST) == B  # the least busy
    assert strategy.fallbacks == 1
    assert "bandit fallback to least connections #1: error: ZeroDivisionError" in caplog.text


@pytest.mark.parametrize("kwargs", [{"epsilon": -0.1}, {"epsilon": 1.5}, {"window": 0}])
def test_rejects_invalid_settings(kwargs):
    with pytest.raises(ValueError):
        EpsilonGreedyStrategy(**kwargs)
