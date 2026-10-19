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
