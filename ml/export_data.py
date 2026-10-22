"""Export training data for the latency model from PostgreSQL.

    python -m ml.export_data                       # every Phase 5-style run (round robin / least connections)
    python -m ml.export_data --batch baseline-20261022T030347Z

One row per request a backend served, with the four features the load
balancer could see when it routed that request (lb/strategies/ml_latency.py,
FEATURES) and the target, its response time.

Where each column comes from:
  - SQL (the `training_data` view from Phase 4, db/queries.sql): every
    request joined with the latest heartbeat its backend sent BEFORE the
    request arrived -> cpu_percent, memory_percent (and the heartbeat's age).
  - A replay in Python, per run and backend, of the two counters the LB keeps
    in lb/stats.py, rebuilt from the request log in time order:
      active_connections    = requests to this backend that had arrived and
                              not yet finished (arrived at received_at, finished
                              response_time_ms later);
      recent_avg_latency_ms = the same EWMA as ServerStats (weight 0.2 on the
                              newest), updated when a request FINISHES.
    Only requests that had finished before this one arrived can be in its
    average: using any later information would be "leakage", a feature the
    LB could not have known, and the model would look better offline than it
    can ever be live. A SQL window like AVG(...) OVER (last 10 rows) would
    leak, because a request that arrived earlier may finish later.
    Each run started a fresh LB, so the replay starts from zero per run.

A heartbeat older than MAX_HEARTBEAT_AGE_S counts as missing, as it does
live, and so does one sent during an earlier run (see EXPORT_SQL); rows with a missing feature are kept (empty) and dropped by train.py,
just as the live strategy falls back to least connections for them.

Output: ml/data/training_data.csv (git-ignored; regenerate with this script).
"""

import argparse
import csv
import heapq
from pathlib import Path

import psycopg

from lb.config import PROJECT_ROOT, database_url
from lb.stats import LATENCY_EWMA_ALPHA
from lb.strategies.ml_latency import FEATURES, MAX_HEARTBEAT_AGE_S

DATA_PATH = PROJECT_ROOT / "ml" / "data" / "training_data.csv"
BASELINE_STRATEGIES = ("round_robin", "least_connections")

# The Phase 4 view does the heartbeat join; we only choose which runs and add
# each run's start (to express time as seconds since the load started).
#
# The view joins on server_id, and every run reuses the same backend ports
# (so the same servers rows): early in a run, "the latest heartbeat before
# this request" can be one the PREVIOUS run's backend sent a second or two
# earlier. That run's LB (and its own heartbeats) ended before this one
# started, so the live LB, fresh for each run, never had it: such a
# heartbeat is flagged here and treated as missing. (Found in review: 3,510
# rows of the first export had one.)
EXPORT_SQL = """
WITH bounds AS (
    -- Runs execute one at a time, so everything recorded before the latest
    -- earlier run ended belongs to an earlier run.
    SELECT id, max(ended_at) OVER (ORDER BY id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM experiment_runs
)
SELECT t.request_id, t.run_id, e.notes::jsonb ->> 'batch' AS batch, e.strategy, e.scenario,
       extract(epoch FROM t.received_at)                 AS received_s,
       extract(epoch FROM t.received_at - e.started_at)  AS t_in_run_s,
       t.server_id, t.status_code,
       t.cpu_percent, t.memory_percent, t.metrics_age_ms,
       coalesce(t.received_at - t.metrics_age_ms * interval '1 millisecond' <= b.prev_end, false)
                                                         AS heartbeat_from_earlier_run,
       t.response_time_ms
FROM training_data t
JOIN experiment_runs e ON e.id = t.run_id
JOIN bounds b ON b.id = t.run_id
WHERE e.strategy = ANY(%(strategies)s)
  AND (%(batch)s::text IS NULL OR e.notes::jsonb ->> 'batch' = %(batch)s)
  AND e.ended_at IS NOT NULL
ORDER BY t.run_id, t.received_at, t.request_id
"""

COLUMNS = ("request_id", "run_id", "batch", "strategy", "scenario", "received_s", "t_in_run_s", "server_id",
           "status_code", *FEATURES, "response_time_ms")


def replay_lb_counters(rows: list[dict]) -> None:
    """Fill in active_connections and recent_avg_latency_ms for the rows of ONE
    run and ONE backend (sorted by arrival), as the LB's ServerStats had them
    the moment each request arrived."""
    in_flight: list[tuple[float, int]] = []  # min-heap of (finish time, row index)
    ewma = None
    for i, row in enumerate(rows):
        now = row["received_s"]
        # Apply every request that finished before this one arrived, in finish order.
        while in_flight and in_flight[0][0] <= now:
            _, j = heapq.heappop(in_flight)
            if rows[j]["status_code"] != 502:  # 502: no answer, so the LB learned no speed
                sample = rows[j]["response_time_ms"]
                ewma = sample if ewma is None else LATENCY_EWMA_ALPHA * sample + (1 - LATENCY_EWMA_ALPHA) * ewma
        row["active_connections"] = len(in_flight)
        row["recent_avg_latency_ms"] = ewma
        heapq.heappush(in_flight, (now + row["response_time_ms"] / 1000, i))


def export(conn: psycopg.Connection, batch: str | None = None,
           strategies: tuple[str, ...] = BASELINE_STRATEGIES) -> list[dict]:
    cur = conn.execute(EXPORT_SQL, {"batch": batch, "strategies": list(strategies)})
    names = [d.name for d in cur.description]
    groups: dict[tuple[int, int], list[dict]] = {}
    for values in cur:
        row = dict(zip(names, values))
        for key in ("received_s", "t_in_run_s", "metrics_age_ms"):
            row[key] = None if row[key] is None else float(row[key])
        if (row["metrics_age_ms"] is None or row["metrics_age_ms"] > MAX_HEARTBEAT_AGE_S * 1000
                or row["heartbeat_from_earlier_run"]):
            row["cpu_percent"] = row["memory_percent"] = None  # stale, someone else's, or none: unknown
        groups.setdefault((row["run_id"], row["server_id"]), []).append(row)
    rows = []
    for group in groups.values():
        replay_lb_counters(group)
        rows.extend(group)
    rows.sort(key=lambda r: (r["run_id"], r["received_s"], r["request_id"]))
    return rows


def write_csv(rows: list[dict], path: Path = DATA_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch", help="only runs of this experiment batch (default: all finished runs)")
    parser.add_argument("--strategies", nargs="+", default=list(BASELINE_STRATEGIES),
                        help="only runs routed by these strategies (default: the two non-ML baselines)")
    parser.add_argument("--out", type=Path, default=DATA_PATH)
    args = parser.parse_args()
    with psycopg.connect(database_url()) as conn:
        rows = export(conn, args.batch, tuple(args.strategies))
    if not rows:
        raise SystemExit("no requests found; run python -m experiments.run_experiments first")
    complete = sum(all(r[f] is not None for f in FEATURES) for r in rows)
    path = write_csv(rows, args.out)
    print(f"saved {len(rows)} rows from {len({r['run_id'] for r in rows})} runs to {path} "
          f"({complete} with every feature known)")


if __name__ == "__main__":
    main()
