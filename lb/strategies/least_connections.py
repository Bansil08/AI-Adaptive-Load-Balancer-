"""Least-connections strategy: pick the backend with the fewest active connections.

"Active" = requests this load balancer has sent to the backend that have not
finished yet (ServerStats.active_connections, updated by every worker).

Why it adapts to slow servers without measuring speed: by Little's law,
requests in flight = arrival rate x time each one takes. A backend that takes
longer holds its requests longer, so its count stays higher and it gets fewer
new ones; a fast backend frees its slots quickly and gets more. Round robin
ignores all of this and gives every backend the same share.

Ties: at low load several backends often sit at 0. Taking the first one would
pile everything onto whichever backend happens to be listed first, so ties are
broken in rotation (round robin among the tied backends).

Known limitations:
  - Herding: two workers choosing at the same instant both see the same
    minimum and both pick it, since a request is only counted once it has
    started. The rotation above spreads exact ties, not near-ties.
  - A backend that FAILS fast (connection refused in ~1 ms) has few active
    connections and so attracts traffic; health checks and the circuit
    breaker are what take it out (see the crash scenario in Phase 5).

Each decision reads a consistent snapshot of all backends' counters under the
read side of the readers-writers lock (lb/rwlock.py): this is the read-heavy
path that lock was built for.
"""

import threading
from typing import TYPE_CHECKING, Sequence

from lb.strategies.base import Backend, LoadBalancingStrategy

if TYPE_CHECKING:
    from lb.http_parser import HTTPRequest
    from lb.stats import ServerStats


class LeastConnectionsStrategy(LoadBalancingStrategy):
    name = "least_connections"

    def __init__(self) -> None:
        self._stats: "ServerStats | None" = None
        self._tie_counter = 0
        self._lock = threading.Lock()  # protects only the tie-break counter

    def attach(self, stats: "ServerStats") -> None:
        self._stats = stats

    def choose(self, backends: Sequence[Backend], request: "HTTPRequest") -> Backend:
        if self._stats is None:
            raise RuntimeError("least connections needs live stats: create it through a Router")
        snapshot = self._stats.snapshot()  # one consistent instant, taken under the read lock
        active = {b: snapshot[b].active_connections if b in snapshot else 0 for b in backends}
        fewest = min(active.values())
        tied = [b for b in backends if active[b] == fewest]
        # The stats lock was released when snapshot() returned: no code path
        # holds it and this lock together.
        with self._lock:
            index = self._tie_counter % len(tied)
            self._tie_counter += 1
        return tied[index]
