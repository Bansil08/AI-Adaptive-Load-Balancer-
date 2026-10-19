"""Smoke test: the database is reachable and the schema has been applied."""

import psycopg
import pytest

from lb.config import database_url

EXPECTED_TABLES = {
    "servers",
    "health_checks",
    "server_metrics",
    "experiment_runs",
    "requests",
    "run_results",
}


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(database_url(), connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(
            f"Cannot connect to PostgreSQL ({exc}). "
            "Did you run `docker compose up -d` and create .env from .env.example?"
        )
    with connection:
        yield connection


@pytest.mark.db
def test_can_connect_and_query(conn):
    assert conn.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.db
def test_all_tables_exist(conn):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    tables = {name for (name,) in rows}
    missing = EXPECTED_TABLES - tables
    assert not missing, f"Missing tables {missing}; run `python scripts/init_db.py`"


@pytest.mark.db
def test_check_constraint_rejects_bad_check_type(conn):
    """The CHECK constraints are real: an invalid check_type must be refused."""
    with pytest.raises(psycopg.errors.CheckViolation):
        server_id = conn.execute(
            "INSERT INTO servers (address, port) VALUES ('smoke-test', 1) RETURNING id"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO health_checks (server_id, check_type, passed) VALUES (%s, 'udp', true)",
            (server_id,),
        )
    # Undo the test server row so the smoke test leaves the database untouched.
    conn.rollback()
