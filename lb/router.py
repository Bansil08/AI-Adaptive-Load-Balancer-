"""Chooses a backend for each request by delegating to a load-balancing strategy."""

from typing import TYPE_CHECKING, Iterable

from lb.stats import ServerStats
from lb.strategies.base import Backend, LoadBalancingStrategy

if TYPE_CHECKING:
    from lb.http_parser import HTTPRequest


class NoBackendAvailable(Exception):
    """There is no backend to send the request to (the proxy answers 503)."""


class Router:
    """Holds the backend list, the active strategy and the shared per-backend stats.

    Phase 3 adds health filtering here (skip backends whose circuit is open)
    and runtime add/remove from the admin endpoint.
    """

    def __init__(self, backends: Iterable[Backend], strategy: LoadBalancingStrategy):
        # A tuple is immutable, so strategies get a consistent snapshot to index into.
        self._backends = tuple(backends)
        self.strategy = strategy
        self.stats = ServerStats(self._backends)

    @property
    def backends(self) -> tuple[Backend, ...]:
        return self._backends

    def choose(self, request: "HTTPRequest") -> Backend:
        if not self._backends:
            raise NoBackendAvailable("no backends configured")
        return self.strategy.choose(self._backends, request)
