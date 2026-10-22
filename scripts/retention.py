"""Retention job: delete raw log rows older than N days (default 7), in small batches.

Why not one big `DELETE FROM requests WHERE received_at < now() - '7 days'`:
  - It is ONE transaction: every matching row stays locked until the very
    end, so anything else that wants those rows waits behind it.
  - It writes its whole change to the write-ahead log at once (can be
    gigabytes), and replicas fall behind replaying it.
  - The deleted rows remain as dead tuples that VACUUM cannot reclaim until
    the transaction ends, so the table and its indexes bloat meanwhile.
  - If it fails or is cancelled at 99%, everything rolls back: all that work
    is wasted and has to be redone.
Deleting `batch_size` rows per transaction and committing each time keeps
every transaction short: locks are held for milliseconds, autovacuum can
reclaim space as it goes, and an interruption loses at most one batch.

run_results (the per-run summaries) and experiment_runs are kept: they are
small and are the reason raw rows can be thrown away.

Usage:
    python scripts/retention.py                 # delete rows older than 7 days
    python scripts/retention.py --days 30 --batch-size 10000
Run it from cron (e.g. hourly) in a real deployment.
"""

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lb.config import database_url  # noqa: E402

# (table, timestamp column). Fixed names, never user input, so formatting them
# into the SQL is safe; the values (cutoff, limit) are passed as parameters.
TABLES = (
    ("requests", "received_at"),
    ("health_checks", "checked_at"),
    ("server_metrics", "recorded_at"),
)


@dataclass(frozen=True)
class RetentionResult:
    table: str
    deleted: int
    batches: int
    seconds: float
    longest_batch_ms: float = 0.0  # the longest any rows stayed locked
    median_batch_ms: float = 0.0


def delete_old_rows(conn: psycopg.Connection, table: str, column: str, days: float,
                    batch_size: int = 5000, pause: float = 0.0) -> RetentionResult:
    """Delete rows older than `days` from one table, `batch_size` rows per transaction.

    Keyset pagination: each batch continues from the last id the previous one
    deleted (`id > after`). The first version asked every batch for "the
    oldest 5000 old rows" from the start of the index, so each batch walked
    past all the rows earlier batches had deleted (dead until VACUUM) and got
    slower: measured 65 ms median, 213 ms worst, slower than one big DELETE.
    """
    # Fixed once, so every batch uses the same cutoff (now() moves on).
    cutoff = conn.execute("SELECT now() - make_interval(secs => %s)", (days * 86400,)).fetchone()[0]
    # Ids grow with time, so old rows occupy ids below the first new row's id.
    # Bounding the scan there keeps the last batch from reading all the new
    # rows to find out there are no more old ones. (A row slightly out of
    # order, written after a newer one, is caught by the next run.)
    upper = conn.execute(f"SELECT coalesce((SELECT id FROM {table} WHERE {column} >= %s ORDER BY id LIMIT 1),"
                         f" (SELECT max(id) + 1 FROM {table}), 0)", (cutoff,)).fetchone()[0]
    sql = (f"DELETE FROM {table} WHERE id IN ("
           f" SELECT id FROM {table} WHERE id > %s AND id < %s AND {column} < %s ORDER BY id LIMIT %s)"
           f" RETURNING id")
    deleted = batches = 0
    after = 0
    batch_ms: list[float] = []
    started = time.perf_counter()
    while True:
        batch_started = time.perf_counter()
        with conn.transaction():  # one short transaction per batch
            ids = [row[0] for row in conn.execute(sql, (after, upper, cutoff, batch_size))]
        if not ids:
            break
        batch_ms.append((time.perf_counter() - batch_started) * 1000)
        deleted += len(ids)
        batches += 1
        after = max(ids)
        if pause:
            time.sleep(pause)  # leave room for the LB's own writes between batches
    return RetentionResult(table, deleted, batches, time.perf_counter() - started,
                           round(max(batch_ms, default=0.0), 1), round(statistics.median(batch_ms), 1) if batch_ms else 0.0)


def run_retention(conninfo: str, days: float = 7, batch_size: int = 5000,
                  pause: float = 0.0) -> list[RetentionResult]:
    # autocommit=True: each `with conn.transaction()` above is a real, separate
    # transaction that commits when its block ends.
    with psycopg.connect(conninfo, autocommit=True) as conn:
        return [delete_old_rows(conn, table, column, days, batch_size, pause) for table, column in TABLES]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=float, default=7, help="keep this many days (default 7)")
    parser.add_argument("--batch-size", type=int, default=5000, help="rows deleted per transaction")
    parser.add_argument("--pause", type=float, default=0.05, help="seconds to sleep between batches")
    args = parser.parse_args()
    if args.days < 0 or args.batch_size < 1:
        parser.error("--days must be >= 0 and --batch-size >= 1")
    for r in run_retention(database_url(), args.days, args.batch_size, args.pause):
        print(f"{r.table}: deleted {r.deleted} rows in {r.batches} batches ({r.seconds:.2f}s, "
              f"transactions: median {r.median_batch_ms} ms, longest {r.longest_batch_ms} ms)")


if __name__ == "__main__":
    main()
