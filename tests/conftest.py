"""Shared fixtures for tests that need PostgreSQL.

They use a SEPARATE database (adaptive_lb_test), created fresh for each test
run with the real schema and views, so tests never touch the data in your
development database.
"""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from lb.config import database_url
from scripts.init_db import apply_schema

TEST_DB = "adaptive_lb_test"
TABLES = "requests, health_checks, server_metrics, run_results, experiment_runs, servers"


@pytest.fixture(scope="session")
def db_url():
    # CREATE/DROP DATABASE can't run inside a transaction, hence autocommit, and
    # can't run while connected to that database, hence the maintenance DB.
    admin_url = make_conninfo(database_url(), dbname="postgres")
    try:
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(f"Cannot connect to PostgreSQL ({exc}). Run `docker compose up -d`.")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = make_conninfo(database_url(), dbname=TEST_DB)
    apply_schema(url)
    yield url
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture
def db(db_url):
    """A connection to an empty test database (every table truncated before the test)."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        yield conn
