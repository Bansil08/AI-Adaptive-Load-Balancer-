"""Fill a database with realistic synthetic log data (for index and retention benchmarks).

All rows are generated inside Postgres with generate_series, which is far
faster than sending a million rows from Python. Shapes match what the load
balancer writes:
  - requests:       `requests` rows spread evenly over the last `days` days,
                    in time order (ids grow with time, as in real use); per
                    server latency ~ its base delay (20/60/150 ms) with noise;
                    ~2% 502s, ~1% retried, ~1% with no backend (503/429).
  - server_metrics: one heartbeat per second per server (as the agents send).
  - health_checks:  a TCP and an HTTP check every 2 s per server.

Usage (DESTROYS the target database's log data; never point it at real data):
    python scripts/seed_db.py --dbname adaptive_lb_bench --requests 1000000
"""

import argparse
import sys
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lb.config import database_url  # noqa: E402

SERVERS = (("127.0.0.1", 9001, "fast", 20.0), ("127.0.0.1", 9002, "medium", 60.0), ("127.0.0.1", 9003, "slow", 150.0))

SEED_REQUESTS = """
INSERT INTO requests (server_id, received_at, method, path, status_code, response_time_ms, retried)
SELECT CASE WHEN g %% 100 = 0 THEN NULL ELSE ids[1 + g %% 3] END,
       -- evenly spaced over the window, oldest first, so ids grow with time
       now() - make_interval(days => %(days)s) + (g::float8 / %(n)s) * make_interval(days => %(days)s),
       CASE WHEN g %% 10 = 0 THEN 'POST' ELSE 'GET' END,
       '/item/' || (g %% 1000),
       CASE WHEN g %% 100 = 0 THEN (CASE WHEN g %% 200 = 0 THEN 429 ELSE 503 END)
            WHEN random() < 0.02 THEN 502 ELSE 200 END,
       CASE WHEN g %% 100 = 0 THEN random() ELSE base[1 + g %% 3] * (0.7 + 0.6 * random()) END,
       random() < 0.01
FROM generate_series(1, %(n)s) AS g,
     (SELECT array_agg(id ORDER BY port) AS ids FROM servers) s,
     (SELECT %(bases)s::float8[] AS base) b
"""

SEED_METRICS = """
INSERT INTO server_metrics (server_id, recorded_at, cpu_percent, memory_percent, active_connections)
SELECT s.id, t, least(100, 5 + 40 * random()), 0.3 + 0.1 * random(), (random() * 8)::int
FROM generate_series(now() - make_interval(days => %(days)s), now(), interval '1 second') AS t
CROSS JOIN servers s
ORDER BY t
"""

SEED_HEALTH = """
INSERT INTO health_checks (server_id, checked_at, check_type, passed, latency_ms)
SELECT s.id, t, c.kind, random() > 0.001, 0.3 + random()
FROM generate_series(now() - make_interval(days => %(days)s), now(), interval '2 seconds') AS t
CROSS JOIN servers s
CROSS JOIN (VALUES ('tcp'), ('http')) AS c(kind)
ORDER BY t
"""


def seed(conninfo: str, requests: int = 1_000_000, days: int = 8) -> dict[str, float]:
    """Replace all log data with synthetic rows. Returns seconds taken per table."""
    timings = {}
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute("TRUNCATE requests, health_checks, server_metrics, run_results, experiment_runs, servers"
                     " RESTART IDENTITY CASCADE")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO servers (address, port, capacity_label) VALUES (%s, %s, %s)",
                            [(host, port, label) for host, port, label, _ in SERVERS])
        params = {"days": days, "n": requests, "bases": [base for *_, base in SERVERS]}
        for table, sql in (("requests", SEED_REQUESTS), ("server_metrics", SEED_METRICS),
                           ("health_checks", SEED_HEALTH)):
            started = time.perf_counter()
            conn.execute(sql, params)
            timings[table] = time.perf_counter() - started
        # Fresh statistics, and the visibility map set, as on a table that has
        # been running for a while (the planner relies on both).
        conn.execute("VACUUM ANALYZE")
    return timings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dbname", required=True, help="database to fill (its log tables are emptied first)")
    parser.add_argument("--requests", type=int, default=1_000_000)
    parser.add_argument("--days", type=int, default=8)
    args = parser.parse_args()
    for table, seconds in seed(make_conninfo(database_url(), dbname=args.dbname), args.requests, args.days).items():
        print(f"{table}: {seconds:.1f}s")


if __name__ == "__main__":
    main()
