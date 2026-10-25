"""Dashboard JSON endpoints, against the test database and a fake admin API."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import dashboard.app as dashboard

pytestmark = pytest.mark.db


@pytest.fixture
def client(db, db_url, monkeypatch):
    monkeypatch.setattr(dashboard, "database_url", lambda: db_url)
    dashboard.app.config["ADMIN_URL"] = "http://127.0.0.1:1"  # nothing listens on port 1
    return dashboard.app.test_client()


def add_server(db, port):
    return db.execute("INSERT INTO servers (address, port) VALUES ('127.0.0.1', %s) RETURNING id",
                      (port,)).fetchone()[0]


def add_run(db, strategy, scenario, batch, p95, avg=10.0, p50=None, p99=None):
    """p50 / p99 None: a run measured before those columns existed."""
    run_id = db.execute("INSERT INTO experiment_runs (strategy, scenario, notes) VALUES (%s, %s, %s) RETURNING id",
                        (strategy, scenario, json.dumps({"batch": batch}))).fetchone()[0]
    db.execute("INSERT INTO run_results (run_id, avg_latency_ms, p50_latency_ms, p95_latency_ms, p99_latency_ms,"
               " error_rate, throughput_rps) VALUES (%s, %s, %s, %s, %s, 0, 90)", (run_id, avg, p50, p95, p99))


def test_live_traffic_has_complete_5s_buckets_only(client, db):
    a = add_server(db, 9001)
    # 10 requests in a complete bucket 20 s ago, and 1 in a bucket that is still to come. (Not
    # simply "now": if a 5 s boundary passed before the API query, that bucket would be complete.)
    for ms in range(1, 11):
        db.execute("INSERT INTO requests (server_id, received_at, method, path, status_code, response_time_ms)"
                   " VALUES (%s, date_bin('5 seconds', now(), TIMESTAMPTZ '2000-01-01') - interval '20 seconds',"
                   " 'GET', '/', 200, %s)", (a, float(ms)))
    db.execute("INSERT INTO requests (server_id, received_at, method, path, status_code, response_time_ms)"
               " VALUES (%s, now() + interval '10 seconds', 'GET', '/', 200, 1)", (a,))
    points = client.get("/api/traffic").get_json()
    assert len(points) == 1
    assert points[0]["server"] == "127.0.0.1:9001"
    assert points[0]["rps"] == 2.0                       # 10 requests / 5 s
    assert points[0]["p95_ms"] == pytest.approx(9.55)    # 1 + 0.95 * 9
    assert points[0]["p50_ms"] == pytest.approx(5.5)     # 1 + 0.5 * 9
    assert points[0]["p99_ms"] == pytest.approx(9.91)    # 1 + 0.99 * 9


def test_backends_falls_back_to_the_database_when_the_lb_is_down(client, db):
    add_server(db, 9001)
    payload = client.get("/api/backends").get_json()
    assert payload["lb_running"] is False
    assert payload["backends"] == [{"address": "127.0.0.1:9001", "healthy": None, "last_status": "up",
                                    "circuit": None}]


def test_backends_passes_the_admin_api_through(client):
    reply = {"strategy": "bandit", "backends": [{"address": "127.0.0.1:9001", "healthy": True,
                                                 "circuit": "closed"}]}

    class Admin(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Admin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        dashboard.app.config["ADMIN_URL"] = f"http://127.0.0.1:{server.server_port}"
        payload = client.get("/api/backends").get_json()
    finally:
        server.shutdown()
        server.server_close()
    assert payload == reply | {"lb_running": True}


def test_experiments_aggregates_mean_sd_and_range_per_cell(client, db):
    for p95 in (10.0, 20.0, 30.0):
        add_run(db, "round_robin", "unequal_servers", "old", p95)
    add_run(db, "bandit", "unequal_servers", "new-partial", 5.0)   # newer, but not every strategy
    batches = client.get("/api/batches").get_json()
    assert [b["batch"] for b in batches] == ["new-partial", "old"]
    data = client.get("/api/experiments?batch=old").get_json()
    [cell] = data["cells"]
    assert cell["runs"] == 3
    assert cell["p95_latency_ms"] == {"mean": 20.0, "sd": 10.0, "min": 10.0, "max": 30.0}


def test_experiments_has_p50_and_p99_and_none_for_older_batches(client, db):
    for p in (10.0, 20.0):
        add_run(db, "round_robin", "unequal_servers", "new", p, p50=p / 2, p99=p * 2)
    add_run(db, "round_robin", "unequal_servers", "old", 10.0)          # no p50 / p99
    [new] = client.get("/api/experiments?batch=new").get_json()["cells"]
    assert new["p50_latency_ms"]["mean"] == 7.5 and new["p99_latency_ms"]["max"] == 40.0
    data = client.get("/api/experiments?batch=old").get_json()
    [old] = data["cells"]
    assert old["p50_latency_ms"] is None and old["p99_latency_ms"] is None   # "—" on the page
    assert old["p95_latency_ms"]["mean"] == 10.0
    assert data["by_percentile"]["p99"] == {"headline": None, "cards": []}
    assert len(data["by_percentile"]["p95"]["cards"]) == 1


def test_experiments_defaults_to_the_newest_complete_batch(client, db):
    for scenario in ("a", "b", "c", "d"):
        for strategy in dashboard.STRATEGY_ORDER:
            add_run(db, strategy, scenario, "complete", 10.0)
    add_run(db, "bandit", "a", "partial", 5.0)
    assert client.get("/api/experiments").get_json()["batch"] == "complete"


def test_database_down_is_a_503_not_a_crash(monkeypatch):
    monkeypatch.setattr(dashboard, "database_url", lambda: "host=127.0.0.1 port=1 dbname=x connect_timeout=1")
    resp = dashboard.app.test_client().get("/api/summary")
    assert resp.status_code == 503 and "database unreachable" in resp.get_json()["error"]


def test_an_empty_database_shows_the_committed_results(client, tmp_path, monkeypatch):
    # A fresh clone: no experiment batches yet, so the committed final batch is shown, labelled as such.
    committed = {"batch": "comparison-x", "cells": [], "headline": {"text": "h"}, "cards": [], "methodology": {}}
    path = tmp_path / "results.json"
    path.write_text(json.dumps(committed))
    monkeypatch.setattr(dashboard, "STATIC_RESULTS", path)
    data = client.get("/api/experiments").get_json()
    assert data == {"batch": "comparison-x", "cells": [], "headline": {"text": "h"}, "cards": [],
                    "source": "docs/data/results.json"}
    monkeypatch.setattr(dashboard, "STATIC_RESULTS", tmp_path / "missing.json")
    assert client.get("/api/experiments").get_json()["batch"] is None
