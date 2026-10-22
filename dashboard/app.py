"""Live dashboard: Flask + Chart.js, reading PostgreSQL and the LB's admin API.

    python -m dashboard.app                 # http://127.0.0.1:5050  (not 5000: macOS AirPlay uses it)
    python -m dashboard.app --port 5051 --admin-url http://127.0.0.1:8081

Two sources, because they hold different things:

  - PostgreSQL (the Phase 4 views in db/queries.sql): what HAS happened,
    requests per second and p95 latency per backend, error rates, and the
    experiment results. The LB's single DB writer puts every request there.
  - The LB's admin API (GET /backends): what IS true right now, i.e. the
    active strategy, each backend's health and circuit-breaker state and its
    in-flight requests. That state lives only in the LB's memory; it is never
    written to the database. If the LB is not running, the page says so and
    falls back to servers.status from the database.

The browser polls the JSON endpoints every few seconds (static/dashboard.js);
the page itself is never re-rendered by the server. Every endpoint opens a
short-lived connection: the dashboard is read-only and low-traffic, so a
connection pool would add complexity for nothing.
"""

import argparse
import json
import os
import statistics
import urllib.error
import urllib.request

import psycopg
from flask import Flask, jsonify, render_template, request
from psycopg.rows import dict_row

from lb.config import database_url

ADMIN_URL = os.environ.get("LB_ADMIN_URL", "http://127.0.0.1:8081").rstrip("/")
ADMIN_TIMEOUT = 1.0  # seconds; a hung LB must not hang the dashboard
STRATEGY_ORDER = ("round_robin", "least_connections", "ml_latency", "bandit")
SCENARIO_ORDER = ("unequal_servers", "sudden_slowdown", "traffic_spike", "server_crash")  # as in the reports

app = Flask(__name__)
app.config["ADMIN_URL"] = ADMIN_URL


def query(sql: str, params=()) -> list[dict]:
    with psycopg.connect(database_url(), connect_timeout=3, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def as_number(value):
    """Postgres numeric -> Decimal, which json can't encode; floats are fine for display."""
    return None if value is None else float(value)


@app.errorhandler(psycopg.OperationalError)
def database_down(exc):
    return jsonify({"error": f"database unreachable: {exc}".strip()}), 503


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/traffic")
def traffic():
    """Requests per second and p95 per backend, 5-second buckets, last 2 minutes."""
    rows = query("SELECT server, bucket, requests_per_second, p95_latency_ms "
                 "FROM dashboard_live_traffic ORDER BY bucket, server")
    return jsonify([{"server": r["server"], "t": r["bucket"].isoformat(),
                     "rps": as_number(r["requests_per_second"]), "p95_ms": as_number(r["p95_latency_ms"])}
                    for r in rows])


@app.get("/api/summary")
def summary():
    """Per backend, last 15 minutes: requests, retries, error rate (Phase 4 views)."""
    counts = {r["server"]: r for r in query("SELECT * FROM dashboard_requests_per_server")}
    out = []
    for r in query("SELECT * FROM dashboard_error_rate ORDER BY server_id NULLS LAST"):
        c = counts.get(r["server"], {})
        if r["server_id"] is not None and c.get("status") == "removed" and not r["requests"]:
            continue  # a backend removed via the admin API, with no recent traffic
        out.append({"server": r["server"], "status": c.get("status"), "requests": r["requests"],
                    "retried": c.get("retried", 0), "errors": r["errors"],
                    "error_rate": as_number(r["error_rate"])})
    return jsonify(out)


@app.get("/api/backends")
def backends():
    """Live LB state from the admin API; servers.status from the DB if the LB is not running."""
    try:
        with urllib.request.urlopen(app.config["ADMIN_URL"] + "/backends", timeout=ADMIN_TIMEOUT) as resp:
            payload = json.load(resp)
        payload["lb_running"] = True
        return jsonify(payload)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        rows = query("SELECT address || ':' || port AS address, status FROM servers "
                     "WHERE status <> 'removed' ORDER BY id")
        return jsonify({"lb_running": False, "error": str(getattr(exc, "reason", exc)), "strategy": None,
                        # Only the last status the health checker wrote: live health is unknown.
                        "backends": [{"address": r["address"], "healthy": None, "last_status": r["status"],
                                      "circuit": None} for r in rows]})


@app.get("/api/batches")
def batches():
    """Experiment batches that have results, newest first."""
    rows = query("""
        SELECT e.notes::jsonb ->> 'batch' AS batch, count(*) AS runs,
               count(DISTINCT e.strategy) AS strategies, count(DISTINCT e.scenario) AS scenarios
        FROM experiment_runs e JOIN run_results rr ON rr.run_id = e.id
        WHERE e.notes IS NOT NULL AND e.notes::jsonb ? 'batch'
        GROUP BY batch ORDER BY max(e.id) DESC""")
    return jsonify(rows)


def default_batch() -> str | None:
    """The newest batch with every strategy on every scenario, else the newest batch."""
    rows = batches().get_json()
    complete = [r for r in rows if r["strategies"] == len(STRATEGY_ORDER) and r["scenarios"] >= len(SCENARIO_ORDER)]
    return (complete or rows or [{"batch": None}])[0]["batch"]


@app.get("/api/experiments")
def experiments():
    """run_results of one batch, aggregated per (scenario, strategy): mean, standard deviation, min, max."""
    batch = request.args.get("batch") or default_batch()
    if batch is None:
        return jsonify({"batch": None, "cells": []})
    rows = query("""
        SELECT e.scenario, e.strategy, rr.avg_latency_ms, rr.p95_latency_ms, rr.error_rate, rr.throughput_rps
        FROM run_results rr JOIN experiment_runs e ON e.id = rr.run_id
        WHERE e.notes::jsonb ->> 'batch' = %s ORDER BY e.id""", (batch,))
    cells: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        cells.setdefault((r["scenario"], r["strategy"]), []).append(r)

    def spread(values):
        return {"mean": statistics.fmean(values), "sd": statistics.stdev(values) if len(values) > 1 else 0.0,
                "min": min(values), "max": max(values)}

    out = []
    for (scenario, strategy), runs in cells.items():
        out.append({"scenario": scenario, "strategy": strategy, "runs": len(runs),
                    **{key: spread([r[key] for r in runs])
                       for key in ("avg_latency_ms", "p95_latency_ms", "error_rate", "throughput_rps")}})
    rank = {name: i for i, name in enumerate(STRATEGY_ORDER + SCENARIO_ORDER)}
    out.sort(key=lambda c: (rank.get(c["scenario"], 99), c["scenario"], rank.get(c["strategy"], 99)))
    return jsonify({"batch": batch, "cells": out})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--admin-url", default=ADMIN_URL, help="the LB's admin API (default %(default)s)")
    args = parser.parse_args()
    app.config["ADMIN_URL"] = args.admin_url.rstrip("/")
    # threaded: one slow query (or a slow admin API) doesn't block other polls.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
