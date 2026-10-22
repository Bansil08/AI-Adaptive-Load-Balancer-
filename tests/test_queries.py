"""The dashboard / training views return the right numbers, and retention deletes only old rows."""

import pytest

from scripts.retention import delete_old_rows, run_retention

pytestmark = pytest.mark.db


def add_server(db, port, status="up"):
    return db.execute("INSERT INTO servers (address, port, status) VALUES ('127.0.0.1', %s, %s) RETURNING id",
                      (port, status)).fetchone()[0]


def add_request(db, server_id, ago, ms=10.0, status=200, retried=False):
    """`ago` is a Postgres interval string, e.g. '2 minutes'."""
    db.execute("INSERT INTO requests (server_id, received_at, method, path, status_code, response_time_ms, retried)"
               " VALUES (%s, now() - %s::interval, 'GET', '/', %s, %s, %s)", (server_id, ago, status, ms, retried))


def test_latency_per_minute_avg_and_p95(db):
    a = add_server(db, 9001)
    for ms in range(1, 21):                 # 1..20 ms, all in the same minute
        add_request(db, a, "2 minutes", ms=float(ms))
    add_request(db, a, "20 minutes", ms=999.0)   # outside the 15-minute window
    rows = db.execute("SELECT server, requests, avg_latency_ms, p95_latency_ms "
                      "FROM dashboard_latency_per_minute").fetchall()
    # p95 by linear interpolation over 20 values: 1 + 0.95 * (20 - 1) = 19.05
    assert [(s, n, float(avg), float(p95)) for s, n, avg, p95 in rows] == [("127.0.0.1:9001", 20, 10.5, 19.05)]


def test_latency_per_minute_groups_by_minute_and_server(db):
    a, b = add_server(db, 9001), add_server(db, 9002)
    add_request(db, a, "1 minute")
    add_request(db, a, "5 minutes")
    add_request(db, b, "5 minutes")
    rows = db.execute("SELECT server_id, requests FROM dashboard_latency_per_minute ORDER BY server_id, minute")
    assert rows.fetchall() == [(a, 1), (a, 1), (b, 1)]


def test_error_rate_per_server_including_unrouted_requests(db):
    a, b = add_server(db, 9001), add_server(db, 9002)
    for status in (200,) * 8 + (502, 504):
        add_request(db, a, "1 minute", status=status)
    add_request(db, a, "1 hour", status=502)     # too old: not counted
    for status in (503, 503, 429):
        add_request(db, None, "1 minute", status=status)
    rows = {server: (n, errors, None if rate is None else float(rate)) for _, server, n, errors, rate in
            db.execute("SELECT * FROM dashboard_error_rate").fetchall()}
    assert rows["127.0.0.1:9001"] == (10, 2, pytest.approx(0.2))
    assert rows["127.0.0.1:9002"] == (0, 0, None)   # idle server: listed, rate undefined
    assert rows["(no backend)"] == (3, 2, pytest.approx(0.6667, abs=1e-4))


def test_requests_per_server_counts_and_retries(db):
    a, b = add_server(db, 9001), add_server(db, 9002, status="down")
    add_request(db, a, "1 minute", retried=True)
    add_request(db, a, "3 minutes")
    add_request(db, b, "30 minutes")
    rows = db.execute("SELECT server, status, requests, retried FROM dashboard_requests_per_server "
                      "ORDER BY server").fetchall()
    assert rows == [("127.0.0.1:9001", "up", 2, 1), ("127.0.0.1:9002", "down", 0, 0)]


def test_training_data_uses_the_latest_metrics_before_each_request(db):
    a, b = add_server(db, 9001), add_server(db, 9002)
    # One transaction, so every now() below is the same instant (now() is the
    # transaction's start time; in autocommit each statement would get its own).
    with db.transaction():
        for ago, cpu in (("10 seconds", 10.0), ("5 seconds", 50.0), ("-5 seconds", 90.0)):  # last one is AFTER
            db.execute("INSERT INTO server_metrics (server_id, recorded_at, cpu_percent, memory_percent,"
                       " active_connections) VALUES (%s, now() - %s::interval, %s, 1, 2)", (a, ago, cpu))
        db.execute("INSERT INTO server_metrics (server_id, recorded_at, cpu_percent, memory_percent,"
                   " active_connections) VALUES (%s, now() - interval '1 second', 77, 1, 2)", (b,))
        add_request(db, a, "0 seconds", ms=42.0)
        add_request(db, b, "1 minute")       # before b's only heartbeat: no metrics yet
        add_request(db, None, "0 seconds", status=503)   # not served by a backend: excluded
    rows = db.execute("SELECT server_id, cpu_percent, round(metrics_age_ms::numeric), response_time_ms "
                      "FROM training_data ORDER BY server_id").fetchall()
    # a: the 50 % heartbeat 5 s before (not the newer 90 % one, which came after).
    assert rows == [(a, 50.0, 5000, 42.0), (b, None, None, 10.0)]


def test_retention_deletes_only_old_rows_in_batches(db, db_url):
    a = add_server(db, 9001)
    for _ in range(10):
        add_request(db, a, "8 days")
    for _ in range(4):
        add_request(db, a, "6 days")
    db.execute("INSERT INTO health_checks (server_id, checked_at, check_type, passed) VALUES"
               " (%s, now() - interval '8 days', 'tcp', true), (%s, now(), 'tcp', true)", (a, a))
    db.execute("INSERT INTO server_metrics (server_id, recorded_at, cpu_percent, memory_percent,"
               " active_connections) VALUES (%s, now() - interval '9 days', 1, 1, 1), (%s, now(), 1, 1, 1)", (a, a))
    result = delete_old_rows(db, "requests", "received_at", days=7, batch_size=3)
    assert (result.deleted, result.batches) == (10, 4)   # 3 + 3 + 3 + 1
    results = {r.table: r.deleted for r in run_retention(db_url, days=7, batch_size=3)}
    assert results == {"requests": 0, "health_checks": 1, "server_metrics": 1}
    counts = db.execute("SELECT (SELECT count(*) FROM requests), (SELECT count(*) FROM health_checks),"
                        " (SELECT count(*) FROM server_metrics), (SELECT count(*) FROM servers)").fetchone()
    assert counts == (4, 1, 1, 1)  # the recent rows, and the server itself, are kept
