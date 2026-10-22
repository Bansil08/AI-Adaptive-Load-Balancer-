"""Chooses a backend for each request and tracks which backends may be used.

A backend is a routing candidate only if it is:
  - healthy        (lb/health.py has not marked it down),
  - not draining   (the admin API is removing it),
  - not tripped    (its circuit breaker lets requests through), and
  - not excluded   (a retry must go to a backend not already tried).
Among the candidates, the strategy (or sticky hashing) picks one.

acquire() / release() bracket every request ("a lease"), so a backend being
removed can wait until its in-flight requests have finished (drain). The
lease holds the backend's state object itself, not just its address: if the
backend is removed and the same address is added again while an old request
is still running, that request's release() must not touch the new entry.

Locking: Router._lock guards the backend list and per-backend state. It is
never held while calling into a breaker or the strategy (each has its own
lock), so no code path here holds two locks at once.
"""

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from lb.circuit_breaker import CircuitBreaker
from lb.config import BreakerSettings
from lb.stats import ServerStats
from lb.strategies.base import Backend, LoadBalancingStrategy

if TYPE_CHECKING:
    from lb.http_parser import HTTPRequest


class NoBackendAvailable(Exception):
    """There is no backend to send the request to (the proxy answers 503)."""


@dataclass
class _BackendState:
    breaker: CircuitBreaker
    healthy: bool = True
    draining: bool = False
    in_flight: int = 0  # leases currently held (requests between acquire and release)
    removed: bool = False


@dataclass(frozen=True)
class Lease:
    """Returned by acquire(); pass it back to release()."""

    backend: Backend
    _state: _BackendState = field(repr=False, compare=False)

    @property
    def removed(self) -> bool:
        """The backend was removed while this request was still running (drain timeout)."""
        return self._state.removed


@dataclass(frozen=True)
class BackendView:
    """Read-only snapshot of one backend's routing state, for the admin API."""

    backend: Backend
    healthy: bool
    draining: bool
    in_flight: int
    breaker_state: str


def sticky_choice(candidates: list[Backend], client_ip: str) -> Backend:
    """Rendezvous (highest-random-weight) hashing on the client IP.

    Every client scores every backend with hash(ip, backend) and takes the
    highest. The same IP always gets the same backend while it is available,
    and when one backend goes away only ITS clients move; with the simpler
    hash(ip) % len(candidates), almost every client would be remapped.
    SHA-256 rather than hash(): Python randomises str hashes per process, so
    the mapping would change on every restart. And not crc32: it is a linear
    checksum, so inputs differing only in the port digits get correlated
    scores (measured: one of three backends got 50% of 600 clients).
    """
    def score(backend: Backend) -> int:
        digest = hashlib.sha256(f"{client_ip}|{backend}".encode()).digest()
        return int.from_bytes(digest[:8], "big")
    return max(candidates, key=score)


class Router:
    def __init__(
        self,
        backends: Iterable[Backend],
        strategy: LoadBalancingStrategy,
        breaker_settings: BreakerSettings | None = None,
        sticky_sessions: bool = False,
    ):
        self.strategy = strategy
        self.sticky_sessions = sticky_sessions
        # None = no circuit breaking: a breaker whose threshold can't be reached.
        self._breaker_settings = breaker_settings or BreakerSettings(failure_threshold=2**31)
        self._lock = threading.Lock()
        # Signalled whenever a lease ends, so drain() can wait for in_flight to hit 0.
        self._lease_ended = threading.Condition(self._lock)
        # Copy-on-write: a new tuple is built on add/remove, so code that grabbed
        # the old one can keep iterating it safely without the lock.
        self._backends: tuple[Backend, ...] = ()
        self._state: dict[Backend, _BackendState] = {}
        for backend in backends:
            self.add_backend(backend)
        self.stats = ServerStats(self._backends)
        strategy.attach(self.stats)

    @property
    def backends(self) -> tuple[Backend, ...]:
        return self._backends

    def set_strategy(self, strategy: LoadBalancingStrategy) -> None:
        """Switch routing strategy at runtime (admin API / dashboard).

        No lock needed: the new strategy is fully built and attached to the
        stats first, then published with one attribute assignment, which is
        atomic in CPython. A request that already read the old strategy just
        finishes with it (its observe() may go to the old one: harmless).
        """
        strategy.attach(self.stats)
        self.strategy = strategy

    # --- choosing ---------------------------------------------------------------

    def choose(self, request: "HTTPRequest", client_ip: str = "",
               exclude: Iterable[Backend] = ()) -> Backend:
        """Pick a candidate without leasing it (no breaker permit, no drain tracking)."""
        candidates = self._candidates(set(exclude))
        if not candidates:
            raise NoBackendAvailable(self._why_none())
        if self.sticky_sessions and client_ip:
            return sticky_choice(candidates, client_ip)
        return self.strategy.choose(candidates, request)

    def acquire(self, request: "HTTPRequest", client_ip: str = "",
                exclude: Iterable[Backend] = ()) -> Lease:
        """Pick a backend and lease it. Every successful acquire() needs a release()."""
        skip = set(exclude)
        while True:
            backend = self.choose(request, client_ip, skip)
            # Lease first, then the breaker permit: giving back a lease is
            # trivial, giving back a half-open trial slot is not.
            state = self._start_lease(backend)
            if state is not None:
                if state.breaker.try_acquire():
                    return Lease(backend, state)
                self._end_lease(state)
            # Lost a race (it started draining, or another thread took the last
            # half-open trial slot): try the remaining candidates.
            skip.add(backend)

    def release(self, lease: Lease, success: bool) -> None:
        """End a lease from acquire(), reporting the outcome to the breaker."""
        if success:
            lease._state.breaker.record_success()
        else:
            lease._state.breaker.record_failure()
        self._end_lease(lease._state)

    def _candidates(self, exclude: set[Backend]) -> list[Backend]:
        with self._lock:
            listed = [(b, self._state[b]) for b in self._backends
                      if b not in exclude and self._state[b].healthy and not self._state[b].draining]
        # Outside the router lock: can_attempt() takes the breaker's own lock.
        return [b for b, state in listed if state.breaker.can_attempt()]

    def _why_none(self) -> str:
        with self._lock:
            if not self._backends:
                return "no backends configured"
            return "no healthy backend available"

    # --- leases and draining ------------------------------------------------------

    def _state_of(self, backend: Backend) -> _BackendState | None:
        with self._lock:
            return self._state.get(backend)

    def _start_lease(self, backend: Backend) -> _BackendState | None:
        with self._lock:
            state = self._state.get(backend)
            # Checked under the same lock drain() uses to set `draining`, so once
            # draining is set no new lease can start: the in-flight count can
            # only go down, and drain() is guaranteed to finish.
            if state is None or state.draining:
                return None
            state.in_flight += 1
            return state

    def _end_lease(self, state: _BackendState) -> None:
        with self._lock:
            state.in_flight -= 1  # this lease's own entry, even if since removed
            self._lease_ended.notify_all()

    # --- runtime changes (health checker, admin API) -----------------------------------

    def add_backend(self, backend: Backend, healthy: bool = True) -> bool:
        """False if it is already present."""
        with self._lock:
            if backend in self._state:
                return False
            self._state[backend] = _BackendState(CircuitBreaker(self._breaker_settings), healthy=healthy)
            self._backends = self._backends + (backend,)
            return True

    def drain_and_remove(self, backend: Backend, timeout: float) -> tuple[bool, int] | None:
        """Stop routing to `backend`, wait up to `timeout` s for its in-flight
        requests, then remove it. Returns (drained cleanly, requests still in
        flight when removed), or None if it wasn't present."""
        deadline = time.monotonic() + timeout
        with self._lock:
            state = self._state.get(backend)
            if state is None or state.draining:
                return None
            state.draining = True
            # Condition wait: releases the lock while sleeping, so the workers
            # finishing those requests can take it to end their leases.
            while state.in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._lease_ended.wait(remaining)
            left = state.in_flight
            state.removed = True
            del self._state[backend]
            self._backends = tuple(b for b in self._backends if b != backend)
        return left == 0, left

    def set_healthy(self, backend: Backend, healthy: bool) -> None:
        with self._lock:
            state = self._state.get(backend)
            if state is not None:
                state.healthy = healthy

    def is_healthy(self, backend: Backend) -> bool:
        state = self._state_of(backend)
        return state is not None and state.healthy

    def views(self) -> list[BackendView]:
        with self._lock:
            listed = [(b, self._state[b]) for b in self._backends]
            rows = [(b, s.healthy, s.draining, s.in_flight, s.breaker) for b, s in listed]
        return [BackendView(b, healthy, draining, in_flight, breaker.state)
                for b, healthy, draining, in_flight, breaker in rows]
