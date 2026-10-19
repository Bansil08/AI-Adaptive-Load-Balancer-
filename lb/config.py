"""Central configuration: reads settings from environment variables (and a local .env file)."""

import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

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

LB_HOST = os.environ.get("LB_HOST", "127.0.0.1")
LB_PORT = int(os.environ.get("LB_PORT", "8080"))
# Comma-separated host:port list, used when no --backend flags are given.
LB_BACKENDS = os.environ.get("LB_BACKENDS", "127.0.0.1:9001,127.0.0.1:9002,127.0.0.1:9003")

# How many fully-established TCP connections the kernel queues for us
# while we are busy and haven't called accept() yet.
LISTEN_BACKLOG = 128

# Size limits so one client can't make us buffer unbounded data in memory.
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 10 * 1024 * 1024

# Timeouts in seconds. Without them a silent peer would hold a thread forever.
CLIENT_TIMEOUT = 10.0
BACKEND_CONNECT_TIMEOUT = 2.0
BACKEND_READ_TIMEOUT = 30.0
