"""Safety net shared by the two ML strategies: fall back to least connections.

A learned strategy can fail in ways a counter-based one cannot: the model
file is missing, a prediction throws, or it is too slow to run on every
request. In all of those cases the request must still be routed, and
routed sensibly, so GuardedStrategy runs the strategy's own choice and
silently swaps in least connections when:

  - the subclass raises NoPrediction (it knows it cannot decide: no model
    loaded, no fresh heartbeat yet, ...),
  - the subclass raises any other exception (a bug or a bad model),
  - the decision took longer than `time_budget_ms`, or
  - it returned a backend that was not among the candidates.

Every fallback is logged (WARNING, with the reason and a running count), so
a strategy that is quietly "least connections in disguise" shows up in the
log; experiments/run_experiments.py counts these lines per run.

The time budget cannot interrupt a slow prediction (Python threads cannot be
pre-empted from outside); it is checked afterwards and the late answer is
thrown away. That still bounds the damage: a model that is slow on every
request is replaced on every request, and the log says so.
"""

import logging
import threading
import time
from typing import TYPE_CHECKING, Sequence

from lb.strategies.base import Backend, LoadBalancingStrategy
from lb.strategies.least_connections import LeastConnectionsStrategy

if TYPE_CHECKING:
    from lb.http_parser import HTTPRequest
    from lb.stats import ServerStats

log = logging.getLogger(__name__)


class NoPrediction(Exception):
    """The strategy cannot make an informed choice right now; use the fallback."""


class GuardedStrategy(LoadBalancingStrategy):
    """Base class: subclasses implement _choose(); choose() adds the fallback."""

    def __init__(self, time_budget_ms: float) -> None:
        self.time_budget_ms = time_budget_ms
        self.fallback = LeastConnectionsStrategy()
        self.fallbacks = 0
        self._fallback_lock = threading.Lock()  # protects only the counter
        self._stats: "ServerStats | None" = None

    def attach(self, stats: "ServerStats") -> None:
        self._stats = stats
        self.fallback.attach(stats)

    def _choose(self, backends: Sequence[Backend], request: "HTTPRequest") -> Backend:
        raise NotImplementedError

    def choose(self, backends: Sequence[Backend], request: "HTTPRequest") -> Backend:
        started = time.perf_counter()
        try:
            choice = self._choose(backends, request)
        except NoPrediction as exc:
            return self._fall_back(backends, request, str(exc))
        except Exception as exc:  # never let a model bug fail the request
            return self._fall_back(backends, request, f"error: {exc!r}")
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > self.time_budget_ms:
            return self._fall_back(backends, request,
                                   f"took {elapsed_ms:.2f} ms, budget {self.time_budget_ms:g} ms")
        if choice not in backends:
            return self._fall_back(backends, request, f"chose {choice}, which is not a candidate")
        return choice

    def _fall_back(self, backends: Sequence[Backend], request: "HTTPRequest", reason: str) -> Backend:
        with self._fallback_lock:
            self.fallbacks += 1
            count = self.fallbacks
        log.warning("%s fallback to least connections #%d: %s", self.name, count, reason)
        return self.fallback.choose(backends, request)
