"""LoadBalancingStrategy interface that every routing strategy implements."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    # Only for type hints: a real import would be circular
    # (http_parser -> config -> strategies.base -> http_parser).
    from lb.http_parser import HTTPRequest
    from lb.stats import ServerStats


@dataclass(frozen=True)
class Backend:
    """One backend server. Frozen (immutable), so threads can share it without locking."""

    host: str
    port: int

    @classmethod
    def parse(cls, text: str) -> "Backend":
        """Parse 'host:port', e.g. '127.0.0.1:9001' or '[::1]:9001' for IPv6."""
        host, sep, port = text.strip().rpartition(":")
        # IPv6 addresses contain ':' themselves, so they are written in brackets.
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not sep or not host or not port.isascii() or not port.isdigit():
            raise ValueError(f"expected host:port, got {text!r}")
        if not 1 <= int(port) <= 65535:
            raise ValueError(f"port out of range in {text!r}")
        return cls(host, int(port))

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


class LoadBalancingStrategy(ABC):
    """Picks which backend serves the next request.

    The proxy only ever calls choose(), so new strategies (least connections,
    ML, bandit) plug in without touching the forwarding code (strategy pattern).
    """

    name: str = "base"

    def attach(self, stats: "ServerStats") -> None:
        """Called once by the Router with the shared live stats. Strategies that
        need them (least connections, ML) keep a reference; others ignore it."""

    @abstractmethod
    def choose(self, backends: Sequence[Backend], request: "HTTPRequest") -> Backend:
        """Return one backend from a non-empty sequence.

        `request` lets a strategy route by content (e.g. path or headers);
        round robin ignores it. Must be thread-safe: many connection threads
        call this concurrently.
        """
