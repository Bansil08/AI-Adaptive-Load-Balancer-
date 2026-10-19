"""Apply db/schema.sql to the running PostgreSQL database.

Usage:
    python scripts/init_db.py           # create any missing tables
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

# Children before parents, so foreign keys never block the drop.
TABLES_DROP_ORDER = [
    "run_results",
    "requests",
    "server_metrics",
    "health_checks",
    "experiment_runs",
    "servers",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="drop all tables before creating them")
    args = parser.parse_args()

    schema_sql = SCHEMA_FILE.read_text()

    # `with connect(...)` commits on success and rolls back on any exception,
    # so the whole schema is applied atomically (Postgres has transactional DDL).
    with psycopg.connect(database_url()) as conn:
        if args.reset:
            for table in TABLES_DROP_ORDER:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
            print("Dropped existing tables.")
        conn.execute(schema_sql)

        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        ).fetchall()

    print(f"Schema applied from {SCHEMA_FILE.relative_to(PROJECT_ROOT)}. Tables:")
    for (name,) in rows:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
