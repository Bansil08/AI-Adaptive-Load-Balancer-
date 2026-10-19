"""Chooses a backend for each request by delegating to a load-balancing strategy."""

from typing import Iterable

from lb.strategies.base import Backend, LoadBalancingStrategy


class NoBackendAvailable(Exception):
    """There is no backend to send the request to (the proxy answers 503)."""


class Router:
    """Holds the backend list and the active strategy.

    Phase 3 adds health filtering here (skip backends whose circuit is open)
    and runtime add/remove from the admin endpoint.
    """

    def __init__(self, backends: Iterable[Backend], strategy: LoadBalancingStrategy):
        # A tuple is immutable, so strategies get a consistent snapshot to index into.
        self._backends = tuple(backends)
        self.strategy = strategy

    @property
    def backends(self) -> tuple[Backend, ...]:
        return self._backends

    def choose(self) -> Backend:
        if not self._backends:
            raise NoBackendAvailable("no backends configured")
        return self.strategy.choose(self._backends)
