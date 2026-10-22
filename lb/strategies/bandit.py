"""Epsilon-greedy multi-armed bandit strategy using negative latency as reward.

The idea in plain words: picture a row of slot machines ("one-armed
bandits"), each paying out differently, and you don't know which is best.
Every backend is an arm; sending a request to it is a pull; the payout
(reward) is minus its response time, so a faster answer is a bigger reward.
Unlike ml_latency there is no model trained in advance: the strategy learns
while it routes, from the outcome of every request (observe(), called by the
proxy after each attempt).

Exploration vs exploitation: always picking the arm that looks best so far
(exploit) never re-checks the others, so if the best one degrades or a
worse one recovers, a purely greedy strategy may never find out. Picking at
random (explore) keeps the knowledge fresh but wastes requests on arms we
already know are slow. Epsilon-greedy mixes the two: with probability
epsilon (e.g. 0.1) pick a random healthy backend, otherwise pick the one
with the best average reward. epsilon is the price paid, permanently, for
being able to notice change: with 3 backends and epsilon = 0.1, about 3.3%
of requests go to EACH backend, the slowest included.

Forgetting: each arm's estimate is the mean of only its last `window`
rewards (a sliding window), so old observations fall out and the estimate
follows a backend that changes. A mean over all history would need ever
more slow samples to outweigh a long fast past.

Failures: a request that fails (refused, reset, timed out, or a 5xx) is
recorded as `failure_penalty_ms`. A refused connection takes ~1 ms, which
would otherwise look like the BEST latency: a dead backend must look
terrible, not great.

Cold start: an arm with no observations yet is tried before any greedy
choice (several at once go to the least loaded untried arm), so every
backend gets an estimate. A backend added at runtime starts this way too.

Thread safety: all arm state is guarded by one lock, held only for a few
dictionary/deque operations; it is never held together with another lock.
"""

import random
import threading
from collections import deque
from typing import TYPE_CHECKING, Sequence

from lb.strategies.base import Backend
from lb.strategies.guarded import GuardedStrategy

if TYPE_CHECKING:
    from lb.config import LBConfig
    from lb.http_parser import HTTPRequest


class _Arm:
    """The last `window` rewards of one backend, with their running sum (O(1) mean)."""

    __slots__ = ("rewards", "total")

    def __init__(self, window: int) -> None:
        self.rewards: deque[float] = deque(maxlen=window)
        self.total = 0.0

    def add(self, reward: float) -> None:
        if len(self.rewards) == self.rewards.maxlen:
            self.total -= self.rewards[0]  # about to be pushed out of the window
        self.rewards.append(reward)
        self.total += reward

    @property
    def mean(self) -> float:
        return self.total / len(self.rewards)


class EpsilonGreedyStrategy(GuardedStrategy):
    name = "bandit"

    def __init__(self, epsilon: float = 0.1, window: int = 50, failure_penalty_ms: float = 1000.0,
                 seed: int | None = None, time_budget_ms: float = 2.0) -> None:
        if not 0 <= epsilon <= 1:
            raise ValueError("epsilon must be between 0 and 1")
        if window < 1:
            raise ValueError("window must be at least 1")
        super().__init__(time_budget_ms)
        self.epsilon = epsilon
        self.window = window
        self.failure_penalty_ms = failure_penalty_ms
        self._rng = random.Random(seed)
        self._arms: dict[Backend, _Arm] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: "LBConfig") -> "EpsilonGreedyStrategy":
        b = cfg.bandit
        return cls(b.epsilon, b.window, b.failure_penalty_ms, b.seed, b.time_budget_ms)

    def observe(self, backend: Backend, latency_ms: float | None) -> None:
        reward = -(self.failure_penalty_ms if latency_ms is None else latency_ms)
        with self._lock:
            arm = self._arms.get(backend)
            if arm is None:
                arm = self._arms[backend] = _Arm(self.window)
            arm.add(reward)

    def estimates(self) -> dict[Backend, float]:
        """Mean reward (minus mean latency in ms) per arm observed so far."""
        with self._lock:
            return {b: arm.mean for b, arm in self._arms.items()}

    def _choose(self, backends: Sequence[Backend], request: "HTTPRequest") -> Backend:
        with self._lock:
            if self._rng.random() < self.epsilon:
                return self._rng.choice(backends)  # explore
            means = {b: self._arms[b].mean for b in backends if b in self._arms}
            untried = [b for b in backends if b not in means]
            if not untried:  # exploit: the best average reward; random among exact ties
                best = max(means.values())
                return self._rng.choice([b for b in backends if means[b] == best])
        # Outside our lock: least connections takes the stats lock.
        return self.fallback.choose(untried, request)
