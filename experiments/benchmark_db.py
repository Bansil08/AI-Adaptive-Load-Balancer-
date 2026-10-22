"""Database benchmarks for Phase 4: insert batching, indexes (EXPLAIN ANALYZE), retention.

Runs in a throwaway database (adaptive_lb_bench), never the development one:

1. Inserts: rows/second for
     - one INSERT + COMMIT per row (what "every worker inserts its own row" costs),
     - one INSERT per row inside one transaction (round trips, but one commit),
     - executemany in one transaction per batch, for several batch sizes
       (what lb/db_writer.py does), and
     - the real DBWriter end to end (submit() from Python -> rows committed).
2. Indexes: seed >= 1M requests, drop the two Phase 4 indexes, run each
   dashboard query with EXPLAIN (ANALYZE, BUFFERS) 3 times, create the
   indexes, run them again. Median execution time and both plans are saved.
3. Retention: delete one day of old rows in batches of 5000 (total time and
   the LONGEST single transaction), then delete another day with one big
   DELETE, to compare how long rows stay locked.

Results: experiments/results/db_benchmark.md (+ .json). Real measured numbers only.

Run:  python -m experiments.benchmark_db                 # ~5 min
      python -m experiments.benchmark_db --requests 200000 --repeats 1
"""

import argparse
import json
import platform
import statistics
import time
from datetime import datetime, timezone

import psycopg
from psycopg.conninfo import make_conninfo

from experiments.benchmark_concurrency import RESULTS_DIR
from lb.config import DatabaseSettings, database_url
from lb.db_writer import INSERT_REQUEST, DBWriter, RequestRecord
from lb.strategies.base import Backend
from scripts.init_db import apply_schema
from scripts.retention import delete_old_rows
from scripts.seed_db import seed

BENCH_DB = "adaptive_lb_bench"
INDEXES = {
    "requests_server_received_idx": "CREATE INDEX requests_server_received_idx ON requests (server_id, received_at)",
    "server_metrics_server_recorded_idx":
        "CREATE INDEX server_metrics_server_recorded_idx ON server_metrics (server_id, recorded_at)",
}
QUERIES = {
    "p95/avg latency per server per minute (15 min)": "SELECT * FROM dashboard_latency_per_minute",
    "error rate per server (15 min)": "SELECT * FROM dashboard_error_rate",
    "request count per server (15 min)": "SELECT * FROM dashboard_requests_per_server",
    "training export, latest 100 requests": "SELECT * FROM training_data ORDER BY request_id DESC LIMIT 100",
}


def fresh_database() -> str:
    admin = make_conninfo(database_url(), dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {BENCH_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {BENCH_DB}")
    url = make_conninfo(database_url(), dbname=BENCH_DB)
    apply_schema(url)
    return url


# --- 1. inserts --------------------------------------------------------------------


def request_rows(n: int, server_id: int) -> list[tuple]:
    now = datetime.now(timezone.utc)
    return [(None, server_id, now, "GET", f"/item/{i % 1000}", 200, 12.5, False) for i in range(n)]


def measure_inserts(url: str, repeats: int) -> list[dict]:
    with psycopg.connect(url, autocommit=True) as conn:
        server_id = conn.execute("INSERT INTO servers (address, port) VALUES ('127.0.0.1', 9001) RETURNING id"
                                 ).fetchone()[0]

        def per_row_commit(rows):
            for row in rows:          # autocommit: every execute is its own transaction
                conn.execute(INSERT_REQUEST, row)

        def per_row_one_tx(rows):
            with conn.transaction():
                for row in rows:      # one round trip per row, but a single commit
                    conn.execute(INSERT_REQUEST, row)

        def batched(size):
            def run(rows):
                for i in range(0, len(rows), size):
                    with conn.transaction(), conn.cursor() as cur:
                        cur.executemany(INSERT_REQUEST, rows[i:i + size])
            return run

        methods = [("one INSERT + COMMIT per row", 3_000, per_row_commit),
                   ("one INSERT per row, one transaction", 20_000, per_row_one_tx)]
        methods += [(f"executemany, batch of {size} per transaction", 50_000, batched(size))
                    for size in (10, 50, 200, 1000)]
        results = []
        for name, n, fn in methods:
            rates = []
            for _ in range(repeats):
                conn.execute("TRUNCATE requests")
                rows = request_rows(n, server_id)
                started = time.perf_counter()
                fn(rows)
                rates.append(n / (time.perf_counter() - started))
                assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == n
            results.append({"method": name, "rows": n, "rows_per_s": [round(r) for r in rates],
                            "median_rows_per_s": round(statistics.median(rates))})
            print(f"  {name:<45} {statistics.median(rates):>9.0f} rows/s", flush=True)

        # The real writer, end to end: from the first submit() to stop() returning.
        rates = []
        n = 100_000
        for _ in range(repeats):
            conn.execute("TRUNCATE requests")
            writer = DBWriter(url, DatabaseSettings(batch_size=200, flush_interval_ms=200, queue_size=n))
            writer.start()
            backend = Backend("127.0.0.1", 9001)
            started = time.perf_counter()
            for i in range(n):
                writer.submit(RequestRecord(time.time(), backend, "GET", f"/item/{i % 1000}", 200, 12.5, False))
            assert writer.stop()
            rates.append(n / (time.perf_counter() - started))
            assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == n
        results.append({"method": "DBWriter end to end (batch 200, 200 ms)", "rows": n,
                        "rows_per_s": [round(r) for r in rates], "median_rows_per_s": round(statistics.median(rates))})
        print(f"  {'DBWriter end to end (batch 200, 200 ms)':<45} {statistics.median(rates):>9.0f} rows/s", flush=True)
        conn.execute("TRUNCATE requests, servers RESTART IDENTITY CASCADE")
    return results


# --- 2. indexes --------------------------------------------------------------------


def explain(conn, sql: str, repeats: int) -> dict:
    times, plan = [], ""
    for _ in range(repeats):
        rows = conn.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}").fetchall()
        plan = "\n".join(r[0] for r in rows)
        times.append(float(plan.rsplit("Execution Time: ", 1)[1].split(" ms")[0]))
    return {"median_ms": round(statistics.median(times), 2), "runs_ms": times, "plan": plan}


def measure_indexes(url: str, repeats: int) -> dict:
    out = {"before": {}, "after": {}}
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SET statement_timeout = '10min'")
        for name in INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("ANALYZE")
        for label, sql in QUERIES.items():
            out["before"][label] = explain(conn, sql, repeats)
            print(f"  before  {label:<50} {out['before'][label]['median_ms']:>10.2f} ms", flush=True)
        out["create_index_s"] = {}
        for name, ddl in INDEXES.items():
            started = time.perf_counter()
            conn.execute(ddl)
            out["create_index_s"][name] = round(time.perf_counter() - started, 2)
        conn.execute("ANALYZE")
        out["index_size"] = dict(conn.execute(
            "SELECT indexrelname, pg_size_pretty(pg_relation_size(indexrelid)) FROM pg_stat_user_indexes"
            " WHERE indexrelname = ANY(%s)", (list(INDEXES),)).fetchall())
        for label, sql in QUERIES.items():
            out["after"][label] = explain(conn, sql, repeats)
            print(f"  after   {label:<50} {out['after'][label]['median_ms']:>10.2f} ms", flush=True)
        # The whole export only becomes practical with the index (one probe per request).
        started = time.perf_counter()
        exported = conn.execute("SELECT count(*), count(cpu_percent) FROM training_data").fetchone()
        out["full_export"] = {"rows": exported[0], "with_metrics": exported[1],
                              "seconds": round(time.perf_counter() - started, 2)}
        print(f"  full training export with index: {exported[0]} rows in {out['full_export']['seconds']} s", flush=True)
    return out


# --- 3. retention ------------------------------------------------------------------


def measure_retention(url: str) -> dict:
    out = {"tables": {}}
    with psycopg.connect(url, autocommit=True) as conn:
        # The real job: rows older than 7 days (the oldest of the 8 seeded days), 5000 per transaction.
        for table, column in (("requests", "received_at"), ("health_checks", "checked_at"),
                              ("server_metrics", "recorded_at")):
            r = delete_old_rows(conn, table, column, days=7, batch_size=5000)
            out["tables"][table] = {"rows": r.deleted, "batches": r.batches, "seconds": round(r.seconds, 2),
                                    "longest_transaction_ms": r.longest_batch_ms,
                                    "median_transaction_ms": r.median_batch_ms}
        # One big DELETE of the next day of requests (about as many rows), for comparison.
        started = time.perf_counter()
        with conn.transaction():
            count = conn.execute("DELETE FROM requests WHERE received_at < now() - interval '6 days'").rowcount
        out["single_delete"] = {"rows": count, "transaction_ms": round((time.perf_counter() - started) * 1000, 1)}
    print(f"  retention: {out}", flush=True)
    return out


# --- report ------------------------------------------------------------------------


def write_markdown(report: dict, path) -> None:
    s, ix, ret = report["setup"], report["indexes"], report["retention"]
    lines = [
        "# Database benchmarks (Phase 4)", "",
        f"Measured {report['measured_at']} by `python -m experiments.benchmark_db`, in a throwaway database "
        f"(`{BENCH_DB}`). Real measured numbers; nothing estimated.", "",
        "## Setup", "",
        f"- Client: {s['machine']}, Python {s['python']}, psycopg {s['psycopg']}",
        f"- Server: {s['postgres']} in Docker Desktop (default settings: shared_buffers {s['shared_buffers']}, "
        f"synchronous_commit {s['synchronous_commit']}), reached over localhost",
        f"- Data for the index runs: {s['rows']['requests']:,} requests, {s['rows']['server_metrics']:,} "
        f"server_metrics, {s['rows']['health_checks']:,} health_checks over {s['days']} days, 3 servers",
        f"- Each query run {s['repeats']}× with `EXPLAIN (ANALYZE, BUFFERS)`; the table shows the median "
        "execution time", "",
        "## 1. Single-row vs batched inserts", "",
        "| Method | Rows | Rows/second (median) |", "|---|---:|---:|",
    ]
    for r in report["inserts"]:
        lines.append(f"| {r['method']} | {r['rows']:,} | {r['median_rows_per_s']:,} |")
    lines += ["", "## 2. Dashboard queries without and with the indexes", "",
              "| Query | Without indexes (ms) | With indexes (ms) | Speed-up |", "|---|---:|---:|---:|"]
    for label in QUERIES:
        b, a = ix["before"][label]["median_ms"], ix["after"][label]["median_ms"]
        lines.append(f"| {label} | {b:,.2f} | {a:,.2f} | {b / a:,.0f}× |")
    fe = ix["full_export"]
    lines += ["",
              f"Index build time: {', '.join(f'`{k}` {v} s' for k, v in ix['create_index_s'].items())}. "
              f"Index size: {', '.join(f'`{k}` {v}' for k, v in ix['index_size'].items())}.", "",
              f"With the index, the **whole** training export ({fe['rows']:,} requests, {fe['with_metrics']:,} "
              f"matched to a heartbeat) took {fe['seconds']} s. Without it, it was not attempted: the 100-row "
              "sample above already shows each request costing a full scan of server_metrics.", "",
              "### Plans", ""]
    for label in QUERIES:
        for when in ("before", "after"):
            lines += [f"<details><summary>{label}: {'without' if when == 'before' else 'with'} indexes</summary>",
                      "", "```", ix[when][label]["plan"], "```", "", "</details>", ""]
    req = ret["tables"]["requests"]
    lines += ["## 3. Retention: batched vs one big DELETE", "",
              "| How | Rows deleted | Transactions | Total time | Each transaction (rows locked this long): median / longest |",
              "|---|---:|---:|---:|---:|",
              f"| Batches of 5000 (`scripts/retention.py`) | {req['rows']:,} | {req['batches']} | {req['seconds']} s | "
              f"{req['median_transaction_ms']} ms / {req['longest_transaction_ms']} ms |",
              f"| One `DELETE` | {ret['single_delete']['rows']:,} | 1 | {ret['single_delete']['transaction_ms'] / 1000:.2f} s | "
              f"{ret['single_delete']['transaction_ms']} ms |", "",
              "Batching does not make the delete faster in total; it makes each transaction short, so rows are "
              "locked, and dead versions pile up, for milliseconds at a time instead of for the whole delete.", "",
              "Other tables, same job (rows older than 7 days): " + ", ".join(
                  f"{t} {v['rows']:,} rows in {v['batches']} batches ({v['seconds']} s, transactions median "
                  f"{v['median_transaction_ms']} ms, longest {v['longest_transaction_ms']} ms)" for t, v in ret["tables"].items() if t != "requests") + ".", ""]
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=1_200_000)
    parser.add_argument("--days", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    url = fresh_database()
    with psycopg.connect(url) as conn:
        postgres = conn.execute("SELECT split_part(version(), ' on ', 1)").fetchone()[0]
        shared_buffers = conn.execute("SHOW shared_buffers").fetchone()[0]
        sync = conn.execute("SHOW synchronous_commit").fetchone()[0]
    print("1. inserts", flush=True)
    inserts = measure_inserts(url, args.repeats)
    print(f"2. seeding {args.requests:,} requests over {args.days} days", flush=True)
    seed(url, args.requests, args.days)
    with psycopg.connect(url) as conn:
        rows = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("requests", "server_metrics", "health_checks")}
    indexes = measure_indexes(url, args.repeats)
    print("3. retention", flush=True)
    retention = measure_retention(url)
    report = {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "setup": {"machine": f"{platform.system()} {platform.release()} {platform.machine()}",
                  "python": platform.python_version(), "psycopg": psycopg.__version__, "postgres": postgres,
                  "shared_buffers": shared_buffers, "synchronous_commit": sync, "rows": rows,
                  "days": args.days, "repeats": args.repeats},
        "inserts": inserts, "indexes": indexes, "retention": retention,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "db_benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    write_markdown(report, RESULTS_DIR / "db_benchmark.md")
    print(f"saved {RESULTS_DIR / 'db_benchmark.md'}")


if __name__ == "__main__":
    main()
