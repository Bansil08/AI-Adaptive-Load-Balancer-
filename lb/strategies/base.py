"""LoadBalancingStrategy interface that every routing strategy implements."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Backend:
    """One backend server. Frozen (immutable), so threads can share it without locking."""

    host: str
    port: int

    @classmethod
    def parse(cls, text: str) -> "Backend":
        """Parse 'host:port', e.g. '127.0.0.1:9001'."""
        host, sep, port = text.strip().rpartition(":")
        if not sep or not host or not port.isascii() or not port.isdigit():
            raise ValueError(f"expected host:port, got {text!r}")
        return cls(host, int(port))

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


class LoadBalancingStrategy(ABC):
    """Picks which backend serves the next request.

    The proxy only ever calls choose(), so new strategies (least connections,
    ML, bandit) plug in without touching the forwarding code (strategy pattern).
    """

    name: str = "base"

    @abstractmethod
    def choose(self, backends: Sequence[Backend]) -> Backend:
        """Return one backend from a non-empty sequence.

        Must be thread-safe: many connection threads call this concurrently.
        """
