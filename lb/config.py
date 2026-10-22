"""Central configuration: reads settings from environment variables (and a local .env file)."""

import math
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

from lb.strategies.base import Backend

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Values already set in the real environment win over .env, so CI or a shell
# export can override the file without editing it.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def database_url() -> str:
    """Build a libpq connection string for psycopg from POSTGRES_* variables."""
    # make_conninfo quotes each value, so a password containing '@', ':' or '/'
    # can't be misread as part of the host or port (as it would in a URL).
    return make_conninfo(
        user=os.environ.get("POSTGRES_USER", "adaptive_lb"),
        password=os.environ.get("POSTGRES_PASSWORD", ""),
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5433"),
        dbname=os.environ.get("POSTGRES_DB", "adaptive_lb"),
    )


# --- Load balancer -----------------------------------------------------------

# Backend addresses, listen port, strategy and timeouts live in this file.
DEFAULT_LB_CONFIG = PROJECT_ROOT / "lb.toml"

# How many fully-established TCP connections the kernel queues for us
# while we are busy and haven't called accept() yet.
LISTEN_BACKLOG = 128

# Size limits so one client can't make us buffer unbounded data in memory.
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 10 * 1024 * 1024

# After replying, how long we keep reading (and discarding) what the client
# still sends before closing, so close() doesn't turn into a TCP reset.
LINGER_SECONDS = 2.0


@dataclass(frozen=True)
class ProxySettings:
    """Timeouts in seconds. Without them a silent peer would hold a thread forever."""

    client_timeout: float = 10.0  # for the WHOLE request (head + body), not per recv()
    backend_connect_timeout: float = 2.0
    backend_read_timeout: float = 30.0


@dataclass(frozen=True)
class ConcurrencySettings:
    """Thread pool and queue sizes, and how long a shutdown waits for in-flight work."""

    workers: int = 32           # fixed number of worker threads
    queue_size: int = 128       # accepted connections waiting for a worker
    shutdown_timeout: float = 10.0  # seconds to let in-flight requests finish


@dataclass(frozen=True)
class HealthSettings:
    """Active health checks (lb/health.py)."""

    interval: float = 2.0          # seconds between probe rounds
    timeout: float = 1.0           # per probe (TCP connect, and the whole HTTP exchange)
    unhealthy_threshold: int = 2   # K: consecutive failed probes before marking down
    healthy_threshold: int = 2     # M: consecutive passed probes before marking up
    path: str = "/health"


@dataclass(frozen=True)
class BreakerSettings:
    """Per-backend circuit breaker (lb/circuit_breaker.py)."""

    failure_threshold: int = 5    # consecutive failed requests that open the circuit
    cooldown: float = 5.0         # seconds to stay OPEN before trying again
    half_open_trials: int = 2     # trial requests let through in HALF_OPEN


@dataclass(frozen=True)
class PoolSettings:
    """Keep-alive connections to backends (lb/connection_pool.py)."""

    size: int = 16               # idle connections kept per backend; 0 disables pooling
    idle_timeout: float = 30.0   # drop connections idle longer than this


@dataclass(frozen=True)
class RetrySettings:
    """Retries of idempotent requests on another backend (lb/proxy.py)."""

    max_retries: int = 0         # extra attempts after the first; 0 disables retries
    backoff_base: float = 0.05   # seconds before the 1st retry; doubles each retry
    backoff_max: float = 1.0     # cap on any single wait


@dataclass(frozen=True)
class RateLimitSettings:
    """Token bucket per client IP (lb/rate_limiter.py)."""

    enabled: bool = True
    rate: float = 100.0          # tokens per second (sustained requests/s per IP)
    burst: float = 200.0         # bucket capacity (largest burst)
    max_clients: int = 10_000    # buckets tracked before idle ones are evicted


@dataclass(frozen=True)
class HeartbeatSettings:
    """UDP listener for backend agents (lb/heartbeat.py)."""

    host: str = "127.0.0.1"
    port: int = 0                # 0 = disabled (lb.toml sets 9100)


@dataclass(frozen=True)
class AdminSettings:
    """Admin HTTP API (lb/admin.py)."""

    host: str = "127.0.0.1"      # loopback: the API has no authentication
    port: int = 0                # 0 = disabled (lb.toml sets 8081)
    drain_timeout: float = 30.0  # max seconds DELETE waits for in-flight requests


@dataclass(frozen=True)
class LBConfig:
    host: str
    port: int
    strategy: str
    backends: tuple[Backend, ...]
    proxy: ProxySettings
    concurrency: ConcurrencySettings = ConcurrencySettings()
    health: HealthSettings = HealthSettings()
    breaker: BreakerSettings = BreakerSettings()
    pool: PoolSettings = PoolSettings()
    retry: RetrySettings = RetrySettings()
    rate_limit: RateLimitSettings = RateLimitSettings()
    heartbeat: HeartbeatSettings = HeartbeatSettings()
    admin: AdminSettings = AdminSettings()
    sticky_sessions: bool = False


# [section] name in lb.toml -> (LBConfig field, settings class)
_SECTIONS = {
    "health": ("health", HealthSettings),
    "circuit_breaker": ("breaker", BreakerSettings),
    "pool": ("pool", PoolSettings),
    "retries": ("retry", RetrySettings),
    "rate_limit": ("rate_limit", RateLimitSettings),
    "heartbeat": ("heartbeat", HeartbeatSettings),
    "admin": ("admin", AdminSettings),
}


def load_lb_config(path: Path | str = DEFAULT_LB_CONFIG) -> LBConfig:
    """Read the load balancer's TOML config file. Raises ValueError on bad values."""
    with open(path, "rb") as f:  # tomllib requires a binary file
        data = tomllib.load(f)
    for section in ("listen", "timeouts", "concurrency", *_SECTIONS):
        if not isinstance(data.get(section, {}), dict):
            raise ValueError(f"{path}: [{section}] must be a table, e.g. [{section}] on its own line")
    listen = data.get("listen", {})
    timeouts = data.get("timeouts", {})
    pool = data.get("concurrency", {})
    defaults = ProxySettings()
    pool_defaults = ConcurrencySettings()
    try:
        entries = data.get("backends", [])
        if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
            raise ValueError('backends must be a list of "host:port" strings')
        backends = tuple(Backend.parse(text) for text in entries)
        proxy = ProxySettings(
            client_timeout=float(timeouts.get("client", defaults.client_timeout)),
            backend_connect_timeout=float(timeouts.get("backend_connect", defaults.backend_connect_timeout)),
            backend_read_timeout=float(timeouts.get("backend_read", defaults.backend_read_timeout)),
        )
        concurrency = ConcurrencySettings(
            workers=_whole_number(pool.get("workers", pool_defaults.workers), "workers"),
            queue_size=_whole_number(pool.get("queue_size", pool_defaults.queue_size), "queue_size"),
            shutdown_timeout=float(pool.get("shutdown_timeout", pool_defaults.shutdown_timeout)),
        )
        port = int(listen.get("port", 8080))
        sections = {attr: _read_section(data.get(name, {}), cls, name) for name, (attr, cls) in _SECTIONS.items()}
        sticky = data.get("sticky_sessions", False)
        if not isinstance(sticky, bool):
            raise ValueError("sticky_sessions must be true or false")
        _check_phase3(**sections)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from None
    # TOML allows inf and nan; socket.settimeout(inf) and Thread.join(inf)
    # raise OverflowError, and every comparison with nan is False.
    seconds = (proxy.client_timeout, proxy.backend_connect_timeout, proxy.backend_read_timeout,
               concurrency.shutdown_timeout)
    if not all(math.isfinite(s) for s in seconds):
        raise ValueError(f"{path}: timeouts must be finite numbers of seconds")
    if min(proxy.client_timeout, proxy.backend_connect_timeout, proxy.backend_read_timeout) <= 0:
        raise ValueError(f"{path}: timeouts must be positive")
    if concurrency.workers < 1 or concurrency.queue_size < 1:
        raise ValueError(f"{path}: workers and queue_size must be at least 1")
    if concurrency.shutdown_timeout < 0:
        raise ValueError(f"{path}: shutdown_timeout must not be negative")
    return LBConfig(
        host=str(listen.get("host", "127.0.0.1")),
        port=port,
        strategy=str(data.get("strategy", "round_robin")),
        backends=backends,
        proxy=proxy,
        concurrency=concurrency,
        sticky_sessions=sticky,
        **sections,
    )


def _read_section(table: dict, cls, name: str):
    """Build a settings dataclass from a TOML table, type-checking each key against its default."""
    known = {f.name: f for f in fields(cls)}
    unknown = set(table) - set(known)
    if unknown:  # most likely a typo, which would otherwise be silently ignored
        raise ValueError(f"[{name}] unknown key(s): {', '.join(sorted(unknown))}")
    values = {}
    for key, value in table.items():
        default = known[key].default
        label = f"[{name}] {key}"
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{label} must be true or false")
        elif isinstance(default, int):
            value = _whole_number(value, label)
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{label} must be a finite number")
            value = float(value)
        elif not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        values[key] = value
    return cls(**values)


def _check_phase3(health, breaker, pool, retry, rate_limit, heartbeat, admin) -> None:
    def require(ok: bool, message: str) -> None:
        if not ok:
            raise ValueError(message)
    require(health.interval > 0 and health.timeout > 0, "[health] interval and timeout must be positive")
    require(health.unhealthy_threshold >= 1 and health.healthy_threshold >= 1,
            "[health] thresholds must be at least 1")
    require(health.path.startswith("/"), "[health] path must start with /")
    require(breaker.failure_threshold >= 1 and breaker.half_open_trials >= 1,
            "[circuit_breaker] failure_threshold and half_open_trials must be at least 1")
    require(breaker.cooldown >= 0, "[circuit_breaker] cooldown must not be negative")
    require(pool.size >= 0 and pool.idle_timeout > 0, "[pool] size must be >= 0 and idle_timeout positive")
    require(retry.max_retries >= 0 and retry.backoff_base >= 0 and retry.backoff_max >= 0,
            "[retries] values must not be negative")
    require(rate_limit.rate > 0 and rate_limit.burst >= 1 and rate_limit.max_clients >= 1,
            "[rate_limit] rate must be positive, burst and max_clients at least 1")
    require(0 <= heartbeat.port <= 65535 and 0 <= admin.port <= 65535, "ports must be 0-65535")
    require(admin.drain_timeout >= 0, "[admin] drain_timeout must not be negative")


def _whole_number(value, name: str) -> int:
    # int(2.7) would silently truncate and int(True) is 1; reject both.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return value
