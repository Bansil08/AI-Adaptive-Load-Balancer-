"""Load-balancing strategies (round robin, least connections, ML latency, bandit)."""

from lb.strategies.base import Backend, LoadBalancingStrategy
from lb.strategies.least_connections import LeastConnectionsStrategy
from lb.strategies.round_robin import RoundRobinStrategy

# Name used on the command line -> strategy class. Later phases add entries here.
STRATEGIES: dict[str, type[LoadBalancingStrategy]] = {
    RoundRobinStrategy.name: RoundRobinStrategy,
    LeastConnectionsStrategy.name: LeastConnectionsStrategy,
}

__all__ = ["Backend", "LeastConnectionsStrategy", "LoadBalancingStrategy", "RoundRobinStrategy", "STRATEGIES"]
