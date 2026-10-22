"""Per-backend circuit breaker (CLOSED -> OPEN -> HALF_OPEN), built with the state pattern.

Health checks vs the circuit breaker:
  - Health checks (lb/health.py) are ACTIVE: a background thread probes every
    backend on a timer, whether or not there is traffic. They notice a dead
    process even when idle, and notice when it comes back.
  - The circuit breaker is PASSIVE: it only looks at the outcome of REAL
    requests. It reacts within a few requests instead of waiting for the next
    probe, and it catches failures that GET /health cannot see (a broken
    endpoint, a backend too overloaded to answer real work in time).
An open breaker also gives a struggling backend a quiet period to recover,
instead of every client retrying into it.

States (each one a class, so its rules live in one place):
  CLOSED     normal. Count consecutive failures; `failure_threshold` in a row -> OPEN.
  OPEN       reject every request for `cooldown` seconds, then -> HALF_OPEN.
  HALF_OPEN  allow at most `half_open_trials` trial requests. One failure -> OPEN
             again (a new cooldown); all trials succeed -> CLOSED.

What counts as a failure: no usable answer (connection refused or reset,
timeout, malformed response) or a 5xx status from the backend.
"""

import threading
import time
from abc import ABC, abstractmethod
from typing import Callable

from lb.config import BreakerSettings


class BreakerState(ABC):
    """One state of the breaker. Methods run with the breaker's lock held."""

    name = "base"

    @abstractmethod
    def can_attempt(self, breaker: "CircuitBreaker", now: float) -> bool:
        """Would a request be let through right now? (No side effects.)"""

    @abstractmethod
    def try_acquire(self, breaker: "CircuitBreaker", now: float) -> bool:
        """Let one request through if allowed, recording that it started."""

    @abstractmethod
    def on_success(self, breaker: "CircuitBreaker", now: float) -> None: ...

    @abstractmethod
    def on_failure(self, breaker: "CircuitBreaker", now: float) -> None: ...


class ClosedState(BreakerState):
    name = "closed"

    def __init__(self) -> None:
        self.failures = 0  # consecutive, so one success resets the count

    def can_attempt(self, breaker, now):
        return True

    def try_acquire(self, breaker, now):
        return True

    def on_success(self, breaker, now):
        self.failures = 0

    def on_failure(self, breaker, now):
        self.failures += 1
        if self.failures >= breaker.settings.failure_threshold:
            breaker._transition(OpenState(now))


class OpenState(BreakerState):
    name = "open"

    def __init__(self, opened_at: float) -> None:
        self.opened_at = opened_at

    def _cooled_down(self, breaker, now) -> bool:
        return now - self.opened_at >= breaker.settings.cooldown

    def can_attempt(self, breaker, now):
        return self._cooled_down(breaker, now)

    def try_acquire(self, breaker, now):
        # The move to HALF_OPEN happens lazily, on the first request after the
        # cooldown, so no timer thread is needed.
        if not self._cooled_down(breaker, now):
            return False
        half_open = HalfOpenState()
        breaker._transition(half_open)
        return half_open.try_acquire(breaker, now)

    def on_success(self, breaker, now):
        pass  # a request that started before the circuit opened; already decided

    def on_failure(self, breaker, now):
        pass


class HalfOpenState(BreakerState):
    name = "half_open"

    def __init__(self) -> None:
        self.started = 0     # trial requests let through so far
        self.succeeded = 0

    def can_attempt(self, breaker, now):
        return self.started < breaker.settings.half_open_trials

    def try_acquire(self, breaker, now):
        if not self.can_attempt(breaker, now):
            return False  # trials are in flight; everyone else waits for their verdict
        self.started += 1
        return True

    def on_success(self, breaker, now):
        self.succeeded += 1
        if self.succeeded >= breaker.settings.half_open_trials:
            breaker._transition(ClosedState())

    def on_failure(self, breaker, now):
        # Still broken: one failed trial is enough to back off for another cooldown.
        breaker._transition(OpenState(now))


class CircuitBreaker:
    """Thread-safe: many workers report outcomes for the same backend at once."""

    def __init__(self, settings: BreakerSettings = BreakerSettings(),
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self._clock = clock  # injectable so tests can move time forward instantly
        self._lock = threading.Lock()
        self._state: BreakerState = ClosedState()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state.name

    def can_attempt(self) -> bool:
        with self._lock:
            return self._state.can_attempt(self, self._clock())

    def try_acquire(self) -> bool:
        """Call right before sending a request; False means "don't send it"."""
        with self._lock:
            return self._state.try_acquire(self, self._clock())

    def record_success(self) -> None:
        with self._lock:
            self._state.on_success(self, self._clock())

    def record_failure(self) -> None:
        with self._lock:
            self._state.on_failure(self, self._clock())

    def _transition(self, state: BreakerState) -> None:
        # Only called by a state, i.e. with self._lock already held.
        self._state = state
