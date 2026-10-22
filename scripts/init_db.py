"""Apply db/schema.sql (tables, indexes) and db/queries.sql (views) to PostgreSQL.

Usage:
    python scripts/init_db.py           # create any missing tables, indexes and views
    python scripts/init_db.py --reset   # drop all project tables first (destroys data)
"""

import argparse
import sys
from pathlib import Path

import psycopg

# Allow `python scripts/init_db.py` from the repo root to import the lb package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lb.config import PROJECT_ROOT, database_url  # noqa: E402

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"
VIEWS_FILE = PROJECT_ROOT / "db" / "queries.sql"

# Children before parents, so foreign keys never block the drop.
TABLES_DROP_ORDER = [
    "run_results",
    "requests",
    "server_metrics",
    "health_checks",
    "experiment_runs",
    "servers",
]


def apply_schema(conninfo: str, reset: bool = False) -> None:
    """Create tables, indexes and views. Atomic: all of it, or (on error) none."""
    # `with connect(...)` commits on success and rolls back on any exception,
    # so the whole schema is applied atomically (Postgres has transactional DDL).
    with psycopg.connect(conninfo) as conn:
        if reset:
            for table in TABLES_DROP_ORDER:
                # CASCADE also drops the views built on these tables.
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            print("Dropped existing tables.")
        conn.execute(SCHEMA_FILE.read_text())
        conn.execute(VIEWS_FILE.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="drop all tables before creating them")
    args = parser.parse_args()

    apply_schema(database_url(), args.reset)
    with psycopg.connect(database_url()) as conn:

        rows = conn.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_type, table_name"
        ).fetchall()

    print(f"Applied {SCHEMA_FILE.relative_to(PROJECT_ROOT)} and {VIEWS_FILE.relative_to(PROJECT_ROOT)}:")
    for name, kind in rows:
        print(f"  - {name}{' (view)' if kind == 'VIEW' else ''}")


if __name__ == "__main__":
    main()
