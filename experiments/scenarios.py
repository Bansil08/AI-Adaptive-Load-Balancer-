"""Scenarios: unequal servers, sudden slowdown, traffic spike, server crash.

Each scenario is data: the backends to start (delay, jitter, capacity), a
request-rate profile, and timed events. experiments/run_experiments.py turns
it into processes, load and actions, so every run is the same experiment.

Every backend has `capacity` = 8: at most 8 requests are worked on at once
and the rest queue, so a backend can serve at most capacity / delay requests
per second (8 / 0.15 s = 53 req/s for the slow one). That is what makes
overload possible; with unlimited capacity a sleep()-based backend would
never get slower however much traffic it got. Jitter is 10% of the delay.

Backends are local processes with artificial delays rather than Docker
containers with CPU limits: delays are exact and repeatable, a process can
be killed at a precise moment, and on macOS Docker CPU limits apply to the
VM's virtual CPUs, which makes them noisy. (`docker compose` still runs the
same backend code for manual testing.)
"""

from dataclasses import dataclass, field, replace

from experiments.load_generator import step_profile


@dataclass(frozen=True)
class BackendSpec:
    name: str
    delay_ms: float
    capacity: int = 8

    @property
    def jitter_ms(self) -> float:
        return self.delay_ms * 0.1

    @property
    def max_rps(self) -> float:
        """Most requests per second it can finish (average delay incl. jitter)."""
        return self.capacity / ((self.delay_ms + self.jitter_ms / 2) / 1000)


@dataclass(frozen=True)
class Event:
    at: float          # seconds after the load starts
    action: str        # "set_delay" or "kill"
    backend: str
    delay_ms: float = 0.0


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    backends: tuple[BackendSpec, ...]
    rate_steps: tuple[tuple[float, float], ...]   # (from second, requests per second)
    duration: float = 30.0
    events: tuple[Event, ...] = field(default=())

    def profile(self):
        return step_profile(list(self.rate_steps))

    def scaled(self, duration: float) -> "Scenario":
        """The same scenario squeezed or stretched to `duration` seconds: events
        and rate steps move proportionally, so a shorter run still contains them."""
        f = duration / self.duration
        return replace(self, duration=duration,
                       rate_steps=tuple((t * f, rate) for t, rate in self.rate_steps),
                       events=tuple(replace(e, at=e.at * f) for e in self.events))


UNEQUAL = (BackendSpec("fast", 20), BackendSpec("medium", 60), BackendSpec("slow", 150))
EQUAL = (BackendSpec("a", 30), BackendSpec("b", 30), BackendSpec("c", 30))

SCENARIOS: dict[str, Scenario] = {
    s.name: s for s in (
        Scenario(
            "unequal_servers",
            "Three backends of different speeds (20 / 60 / 150 ms), steady 90 req/s. Round robin gives the "
            "slow one a third of the traffic (30 req/s, ~59% of its 51 req/s capacity).",
            UNEQUAL, ((0, 90),),
        ),
        Scenario(
            "sudden_slowdown",
            "Three equal 30 ms backends at 90 req/s; at t = 15 s backend b becomes ten times slower "
            "(300 ms). Its capacity drops to ~25 req/s, below the 30 req/s round robin keeps sending it.",
            EQUAL, ((0, 90),),
            events=(Event(15, "set_delay", "b", 300),),
        ),
        Scenario(
            "traffic_spike",
            "Unequal backends (20 / 60 / 150 ms); the rate jumps from 30 to 180 req/s at t = 10 s. A third "
            "of 180 (60 req/s) exceeds the slow backend's ~51 req/s capacity.",
            UNEQUAL, ((0, 30), (10, 180)),
        ),
        Scenario(
            "server_crash",
            "Three equal 30 ms backends at 90 req/s; backend b is killed (SIGKILL) at t = 15 s and not "
            "restarted.",
            EQUAL, ((0, 90),),
            events=(Event(15, "kill", "b"),),
        ),
    )
}
