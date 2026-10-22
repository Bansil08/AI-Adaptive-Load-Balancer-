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
short-lived connection: the dashboard is low-traffic, so a connection pool
would add complexity for nothing.

Live controls (POST, all under /api/control/):
  strategy  {"strategy": "ml_latency"}        -> the LB's admin API, POST /strategy
  fault     {"backend": "host:port", "type": "slow" | "crash" | "restore", "ms": 150}
                                              -> that backend's POST /fault
  load      {"rate": 60}  or  {"rate": 0}     start / change / stop the demo load (load.py)
Each one is recorded as an event (GET /api/events) so the charts can mark it.
The dashboard holds the admin token (ADMIN_TOKEN) and adds it to the calls it
forwards; the browser never sees it.

Safety: a fault is only sent to an address the LB itself lists as a backend,
so the dashboard can't be used to send requests to arbitrary hosts (SSRF).
Control requests must be JSON from this page's own origin: a cross-site form
can't send JSON without a CORS preflight this app never answers, and the
Origin check refuses any other page that tries. They must also be addressed
to a known host name (DASHBOARD_HOSTS, default the loopback names): otherwise
a site whose DNS name is re-pointed at 127.0.0.1 ("DNS rebinding") would be
same-origin with itself and pass the Origin check.
"""

import argparse
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import psycopg
from flask import Flask, jsonify, render_template, request
from psycopg.rows import dict_row

from dashboard.load import DemoLoad
from dashboard.summary import headline, scenario_cards
from lb.config import PROJECT_ROOT, database_url

ADMIN_URL = os.environ.get("LB_ADMIN_URL", "http://127.0.0.1:8081").rstrip("/")
LB_URL = os.environ.get("LB_URL", "http://127.0.0.1:8080").rstrip("/")
ADMIN_TIMEOUT = 1.0     # seconds; a hung LB must not hang the dashboard
CONTROL_TIMEOUT = 15.0  # a strategy switch loads the ML model; a fault call is quick
EVENT_WINDOW_S = 180    # events older than the charts' window are forgotten
# Host names the controls may be addressed to (the port is ignored).
CONTROL_HOSTS = {h.strip() for h in os.environ.get("DASHBOARD_HOSTS", "localhost,127.0.0.1,[::1]").split(",")}
# The committed final batch (written by experiments/export_static.py), shown when
# this database has no experiment results of its own yet, e.g. on a fresh clone.
STATIC_RESULTS = PROJECT_ROOT / "docs" / "data" / "results.json"
STRATEGY_ORDER = ("round_robin", "least_connections", "ml_latency", "bandit")
SCENARIO_ORDER = ("unequal_servers", "sudden_slowdown", "traffic_spike", "server_crash")  # as in the reports

app = Flask(__name__)
app.config["ADMIN_URL"] = ADMIN_URL
app.config["ADMIN_TOKEN"] = os.environ.get("ADMIN_TOKEN", "")
app.config["LOAD"] = DemoLoad(LB_URL)

# Recent control actions, newest last: (epoch seconds, label). Written by
# request threads (Flask is threaded), hence the lock.
_events: deque[tuple[float, str]] = deque(maxlen=200)
_events_lock = threading.Lock()


def record_event(label: str) -> None:
    with _events_lock:
        _events.append((time.time(), label))


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
    """One batch: cells per (scenario, strategy), plus the headline and the winner cards."""
    batch = request.args.get("batch") or default_batch()
    if batch is None:
        if STATIC_RESULTS.exists():
            data = json.loads(STATIC_RESULTS.read_text())
            return jsonify({key: data[key] for key in ("batch", "cells", "headline", "cards")}
                           | {"source": "docs/data/results.json"})
        return jsonify({"batch": None, "cells": [], "headline": None, "cards": []})
    cells = batch_cells(batch)
    return jsonify({"batch": batch, "cells": cells, "headline": headline(cells), "cards": scenario_cards(cells)})


def batch_cells(batch: str) -> list[dict]:
    """run_results of one batch, aggregated per (scenario, strategy): mean, standard deviation, min, max."""
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
    return out


# --- live controls ---------------------------------------------------------------------

class ControlError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@app.errorhandler(ControlError)
def control_error(exc: ControlError):
    return jsonify({"error": str(exc)}), exc.status


def control_body() -> dict:
    """The JSON body of a control request, after the host and same-origin checks."""
    hostname = request.host.rsplit(":", 1)[0] if not request.host.endswith("]") else request.host
    if hostname not in CONTROL_HOSTS:
        raise ControlError(403, f"controls only answer on {sorted(CONTROL_HOSTS)} (set DASHBOARD_HOSTS)")
    origin = request.headers.get("Origin")
    # Browsers send Origin on every cross-site POST; it must be this page's
    # own scheme://host:port. (curl and `make demo` send none: allowed.)
    if origin is not None and origin != request.host_url.rstrip("/"):
        raise ControlError(403, "control requests must come from the dashboard page itself")
    body = request.get_json(silent=True)  # None unless Content-Type is application/json
    if not isinstance(body, dict):
        raise ControlError(400, "send a JSON object with Content-Type: application/json")
    return body


def call_json(method: str, url: str, payload: dict, timeout: float) -> dict:
    """POST JSON with the admin token; the target's JSON answer, or a ControlError with its message."""
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "X-Admin-Token": app.config["ADMIN_TOKEN"]}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method),
                                    timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error", exc.reason)
        except ValueError:
            message = exc.reason
        raise ControlError(exc.code if exc.code < 500 else 502, f"{url}: {message}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ControlError(502, f"{url}: {getattr(exc, 'reason', exc)}") from None


@app.post("/api/control/strategy")
def control_strategy():
    name = control_body().get("strategy")
    if not isinstance(name, str):
        raise ControlError(400, 'body must be like {"strategy": "ml_latency"}')
    result = call_json("POST", app.config["ADMIN_URL"] + "/strategy", {"strategy": name}, CONTROL_TIMEOUT)
    record_event(f"strategy → {name.replace('_', ' ')}")
    return jsonify(result)


@app.post("/api/control/fault")
def control_fault():
    body = control_body()
    backend, kind = body.get("backend"), body.get("type")
    if kind not in ("slow", "crash", "restore"):
        raise ControlError(400, 'type must be "slow", "crash" or "restore"')
    # Only addresses the LB routes to: never an arbitrary host from the request.
    try:
        with urllib.request.urlopen(app.config["ADMIN_URL"] + "/backends", timeout=ADMIN_TIMEOUT) as resp:
            known = {b["address"] for b in json.load(resp)["backends"]}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        raise ControlError(502, f"cannot ask the LB for its backends: {getattr(exc, 'reason', exc)}") from None
    if backend not in known:
        raise ControlError(400, f"{backend!r} is not one of the LB's backends")
    payload = {"type": kind}
    if kind == "slow":
        try:
            payload["ms"] = float(body.get("ms", 150))
        except (TypeError, ValueError):
            raise ControlError(400, "ms must be a number") from None
    result = call_json("POST", f"http://{backend}/fault", payload, CONTROL_TIMEOUT)
    port = backend.rsplit(":", 1)[-1]
    record_event({"slow": f"{port} slowed +{payload.get('ms', 0):.0f} ms", "crash": f"{port} crashed",
                  "restore": f"{port} restored"}[kind])
    return jsonify(result)


@app.get("/api/control/faults")
def control_faults():
    """Each backend's current fault (GET /fault needs no token), for the buttons' state."""
    try:
        with urllib.request.urlopen(app.config["ADMIN_URL"] + "/backends", timeout=ADMIN_TIMEOUT) as resp:
            addresses = [b["address"] for b in json.load(resp)["backends"]]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return jsonify({})
    faults = {}
    for address in addresses:
        try:
            with urllib.request.urlopen(f"http://{address}/fault", timeout=ADMIN_TIMEOUT) as resp:
                faults[address] = json.load(resp).get("fault", "none")
        except (urllib.error.URLError, OSError, ValueError):
            faults[address] = None  # unreachable, or a backend without --allow-control
    return jsonify(faults)


@app.get("/api/control/load")
def load_status():
    return jsonify(app.config["LOAD"].status())


@app.post("/api/control/load")
def control_load():
    try:
        rate = float(control_body().get("rate", 0))
    except (TypeError, ValueError):
        raise ControlError(400, "rate must be a number") from None
    load = app.config["LOAD"]
    if rate <= 0:
        load.stop()
        record_event("load stopped")
    else:
        try:
            load.start(rate)
        except ValueError as exc:
            raise ControlError(400, str(exc)) from None
        record_event(f"load {rate:g} req/s")
    return jsonify(load.status())


@app.get("/api/events")
def events():
    cutoff = time.time() - EVENT_WINDOW_S
    with _events_lock:
        recent = [{"t": t, "label": label} for t, label in _events if t >= cutoff]
    return jsonify(recent)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--admin-url", default=ADMIN_URL, help="the LB's admin API (default %(default)s)")
    parser.add_argument("--lb-url", default=LB_URL, help="where the demo load is sent (default %(default)s)")
    args = parser.parse_args()
    app.config["ADMIN_URL"] = args.admin_url.rstrip("/")
    app.config["LOAD"] = DemoLoad(args.lb_url)
    # threaded: one slow query (or a slow admin API) doesn't block other polls.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
