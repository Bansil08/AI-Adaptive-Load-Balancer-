"""Central configuration: reads settings from environment variables (and a local .env file)."""

import os
import tomllib
from dataclasses import dataclass
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

    client_timeout: float = 10.0
    backend_connect_timeout: float = 2.0
    backend_read_timeout: float = 30.0


@dataclass(frozen=True)
class ConcurrencySettings:
    """Thread pool and queue sizes, and how long a shutdown waits for in-flight work."""

    workers: int = 32           # fixed number of worker threads
    queue_size: int = 128       # accepted connections waiting for a worker
    shutdown_timeout: float = 10.0  # seconds to let in-flight requests finish


@dataclass(frozen=True)
class LBConfig:
    host: str
    port: int
    strategy: str
    backends: tuple[Backend, ...]
    proxy: ProxySettings
    concurrency: ConcurrencySettings = ConcurrencySettings()


def load_lb_config(path: Path | str = DEFAULT_LB_CONFIG) -> LBConfig:
    """Read the load balancer's TOML config file. Raises ValueError on bad values."""
    with open(path, "rb") as f:  # tomllib requires a binary file
        data = tomllib.load(f)
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
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from None
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
    )


def _whole_number(value, name: str) -> int:
    # int(2.7) would silently truncate and int(True) is 1; reject both.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number, got {value!r}")
    return value
