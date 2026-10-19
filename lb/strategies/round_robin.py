"""Round-robin strategy: cycle through healthy backends in order."""

import threading
from typing import Sequence

from lb.strategies.base import Backend, LoadBalancingStrategy


class RoundRobinStrategy(LoadBalancingStrategy):
    name = "round_robin"

    def __init__(self) -> None:
        self._counter = 0
        self._lock = threading.Lock()

    def choose(self, backends: Sequence[Backend]) -> Backend:
        # Reading the counter and incrementing it must happen as one atomic step.
        # Without the lock, two threads can read the same value and both pick the
        # same backend (a lost-update race condition).
        with self._lock:
            index = self._counter % len(backends)
            self._counter += 1
        return backends[index]
