"""Load-balancing strategies (round robin, least connections, ML latency, bandit)."""

from lb.strategies.bandit import EpsilonGreedyStrategy
from lb.strategies.base import Backend, LoadBalancingStrategy
from lb.strategies.least_connections import LeastConnectionsStrategy
from lb.strategies.ml_latency import MLLatencyStrategy
from lb.strategies.round_robin import RoundRobinStrategy

# Name used in lb.toml -> strategy class (built with its from_config()).
STRATEGIES: dict[str, type[LoadBalancingStrategy]] = {
    RoundRobinStrategy.name: RoundRobinStrategy,
    LeastConnectionsStrategy.name: LeastConnectionsStrategy,
    MLLatencyStrategy.name: MLLatencyStrategy,
    EpsilonGreedyStrategy.name: EpsilonGreedyStrategy,
}

__all__ = ["Backend", "EpsilonGreedyStrategy", "LeastConnectionsStrategy", "LoadBalancingStrategy",
           "MLLatencyStrategy", "RoundRobinStrategy", "STRATEGIES"]
