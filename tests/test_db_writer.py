"""DB writer: size and time triggers, flush on shutdown, rollback of a failed batch, reconnects."""

import threading
import time

import psycopg
import pytest

from lb.config import DatabaseSettings
from lb.db_writer import DBWriter, RequestRecord
from lb.strategies.base import Backend
from tests.test_proxy import unused_port

A = Backend("127.0.0.1", 9001)
B = Backend("127.0.0.1", 9002)


class RecordingWriter(DBWriter):
    """Replaces the database write with a list of (time, batch size): batching logic only."""

    def __init__(self, settings):
        super().__init__("unused", settings)
        self.batches: list[tuple[float, int]] = []
        self.batch_written = threading.Event()

    def _write_with_retry(self, batch):
        self.batches.append((time.monotonic(), len(batch)))
        self.batch_written.set()


def request_record(i=0, status=200, backend=A) -> RequestRecord:
    return RequestRecord(time.time(), backend, "GET", f"/{i}", status, 1.5, False)


def test_size_trigger_writes_as_soon_as_a_batch_is_full():
    writer = RecordingWriter(DatabaseSettings(batch_size=10, flush_interval_ms=60_000))
    writer.start()
    started = time.monotonic()
    for i in range(25):
        writer.submit(request_record(i))
    deadline = time.monotonic() + 2
    while len(writer.batches) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    # Two full batches went out at once, long before the 60 s time trigger.
    assert [size for _, size in writer.batches] == [10, 10]
    assert writer.batches[1][0] - started < 1
    writer.stop()


def test_time_trigger_writes_a_partial_batch_after_the_interval():
    writer = RecordingWriter(DatabaseSettings(batch_size=1000, flush_interval_ms=150))
    writer.start()
    started = time.monotonic()
    for i in range(3):
        writer.submit(request_record(i))
    assert writer.batch_written.wait(2)
    elapsed = writer.batches[0][0] - started
    assert writer.batches[0][1] == 3
    assert 0.12 < elapsed < 0.6  # ~150 ms after the first record, not before
    writer.stop()


def test_stop_flushes_everything_still_queued():
    writer = RecordingWriter(DatabaseSettings(batch_size=20, flush_interval_ms=60_000))
    writer.start()
    for i in range(50):
        writer.submit(request_record(i))
    assert writer.stop() is True
    assert sum(size for _, size in writer.batches) == 50
    assert not writer._thread.is_alive()


def test_submit_never_blocks_and_drops_when_the_queue_is_full():
    writer = RecordingWriter(DatabaseSettings(queue_size=5))  # not started: nothing drains
    started = time.monotonic()
    accepted = [writer.submit(request_record(i)) for i in range(8)]
    assert time.monotonic() - started < 0.1
    assert accepted == [True] * 5 + [False] * 3
    counters = writer.counters()
    assert (counters.queued, counters.dropped) == (5, 3)


def test_submit_after_stop_is_dropped_not_an_error():
    writer = RecordingWriter(DatabaseSettings())
    writer.start()
    writer.stop()
    assert writer.submit(request_record()) is False


def test_database_down_never_blocks_producers_and_stop_gives_up_in_time():
    url = f"host=127.0.0.1 port={unused_port()} dbname=x user=x connect_timeout=1"
    writer = DBWriter(url, DatabaseSettings(batch_size=5, flush_interval_ms=50, shutdown_timeout=0.5))
    writer.start()
    started = time.monotonic()
    for i in range(20):
        writer.submit(request_record(i))
    assert time.monotonic() - started < 0.1  # producers are never slowed by the database
    time.sleep(0.2)
    started = time.monotonic()
    assert writer.stop() is False  # could not write; reported, not hung
    assert time.monotonic() - started < 3
    assert writer.counters().written == 0


# --- against the real (test) database ----------------------------------------------


def run_writer(db_url, records, settings=DatabaseSettings(batch_size=500, flush_interval_ms=50), **kw):
    writer = DBWriter(db_url, settings, **kw)
    writer.start()
    for r in records:
        writer.submit(r)
    assert writer.stop()
    return writer


@pytest.mark.db
def test_all_record_types_are_written(db, db_url):
    now = time.time()
    writer = DBWriter(db_url, DatabaseSettings(flush_interval_ms=50))
    writer.start()
    writer.log_request(A, "GET", "/a", 200, 12.5, False, now)
    writer.log_request(None, "POST", "/b", 503, 0.4, False, now)       # no backend: NULL server_id
    writer.log_request(B, "GET", "/c", 200, 30.0, True, now)
    writer.log_health_check(A, "tcp", True, 0.8)
    writer.log_health_check(A, "http", False, None)                    # timed out: NULL latency
    writer.log_metric(B, 12.5, 3.25, 4)
    writer.log_status(A, "down")
    assert writer.stop()

    servers = db.execute("SELECT address, port, status FROM servers ORDER BY port").fetchall()
    assert servers == [("127.0.0.1", 9001, "down"), ("127.0.0.1", 9002, "up")]  # one row each
    rows = db.execute(
        "SELECT s.port, r.method, r.path, r.status_code, r.response_time_ms, r.retried "
        "FROM requests r LEFT JOIN servers s ON s.id = r.server_id ORDER BY r.path").fetchall()
    assert rows == [(9001, "GET", "/a", 200, 12.5, False), (None, "POST", "/b", 503, 0.4, False),
                    (9002, "GET", "/c", 200, 30.0, True)]
    assert db.execute("SELECT check_type, passed, latency_ms FROM health_checks ORDER BY id").fetchall() == \
        [("tcp", True, 0.8), ("http", False, None)]
    assert db.execute("SELECT cpu_percent, memory_percent, active_connections FROM server_metrics").fetchall() == \
        [(12.5, 3.25, 4)]
    # The time the request arrived is kept, not the time the batch was written.
    stored = db.execute("SELECT extract(epoch FROM received_at) FROM requests WHERE path = '/a'").fetchone()[0]
    assert abs(float(stored) - now) < 0.001


@pytest.mark.db
def test_a_batch_is_one_transaction(db, db_url):
    writer = run_writer(db_url, [request_record(i) for i in range(300)],
                        DatabaseSettings(batch_size=300, flush_interval_ms=5_000))
    assert writer.counters().batches == 1 and writer.counters().written == 300
    # All 300 rows were inserted by the same transaction (same xmin).
    assert db.execute("SELECT count(DISTINCT xmin::text) FROM requests").fetchone()[0] == 1


@pytest.mark.db
def test_failed_batch_is_rolled_back_and_the_writer_survives(db, db_url):
    """One row violates a CHECK (status 700). Postgres aborts the whole batch;
    the writer rolls it back, re-inserts the good rows one by one (SAVEPOINTs),
    drops only the bad one, and keeps serving later batches."""
    batch = [request_record(i) for i in range(10)]
    batch[5] = request_record(5, status=700)
    writer = DBWriter(db_url, DatabaseSettings(batch_size=10, flush_interval_ms=5_000))
    writer.start()
    for r in batch:
        writer.submit(r)
    deadline = time.monotonic() + 5
    while writer.counters().written < 9 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert writer._thread.is_alive(), "the writer thread died"
    # Exactly 9: if the failed transaction had NOT been rolled back, the rows
    # before the bad one would now be in the table twice.
    paths = [p for (p,) in db.execute("SELECT path FROM requests ORDER BY id")]
    assert paths == [f"/{i}" for i in range(10) if i != 5]
    for i in range(10, 13):
        writer.submit(request_record(i))
    assert writer.stop()
    counters = writer.counters()
    assert (counters.failed_batches, counters.rejected, counters.written) == (1, 1, 12)
    assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 12


@pytest.mark.db
def test_lost_connection_is_reopened_and_the_batch_is_not_lost(db, db_url):
    def connect(url):
        return psycopg.connect(url, autocommit=True, application_name="db-writer-test")

    writer = DBWriter(db_url, DatabaseSettings(batch_size=5, flush_interval_ms=30), connect=connect)
    writer.start()
    for i in range(5):
        writer.submit(request_record(i))
    deadline = time.monotonic() + 5
    while writer.counters().written < 5 and time.monotonic() < deadline:
        time.sleep(0.02)
    # Kill the writer's connection from the server side, as a DB restart would.
    killed = db.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE application_name = 'db-writer-test'").fetchall()
    assert killed == [(True,)]
    for i in range(5, 10):
        writer.submit(request_record(i))
    assert writer.stop()
    assert writer.counters().reconnects == 1
    assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 10


@pytest.mark.db
def test_server_rows_survive_a_rolled_back_batch(db, db_url):
    """The server row is created in its own transaction before the batch, so a
    batch that fails can't leave the writer caching an id that doesn't exist."""
    new = Backend("10.0.0.9", 80)
    run_writer(db_url, [request_record(0, status=700, backend=new), request_record(1, backend=new)])
    assert db.execute("SELECT count(*) FROM servers WHERE address = '10.0.0.9'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1


# --- wiring: the LB components hand the writer the right records -------------------


class FakeDB:
    """Stands in for DBWriter; records what each component logs."""

    def __init__(self):
        self.requests, self.checks, self.metrics, self.statuses = [], [], [], []

    def log_request(self, *args):
        self.requests.append(args)

    def log_health_check(self, *args):
        self.checks.append(args)

    def log_metric(self, *args):
        self.metrics.append(args)

    def log_status(self, *args):
        self.statuses.append(args)


def test_proxy_logs_served_unrouted_and_rate_limited_requests():
    from lb.config import RateLimitSettings, RetrySettings
    from lb.proxy import Proxy
    from lb.rate_limiter import RateLimiter
    from lb.router import Router
    from lb.server import LoadBalancer
    from lb.strategies.round_robin import RoundRobinStrategy
    from tests.test_proxy import as_backend, request, start_backend

    good = start_backend("good")
    dead = Backend("127.0.0.1", unused_port())
    router = Router([dead, as_backend(good)], RoundRobinStrategy())
    fake = FakeDB()
    limiter = RateLimiter(RateLimitSettings(rate=0.001, burst=3))
    proxy = Proxy(router, retry=RetrySettings(max_retries=1, backoff_base=0), rate_limiter=limiter, db=fake)
    lb = LoadBalancer(router, "127.0.0.1", 0, proxy=proxy)
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        before = time.time()
        assert request(port, path="/x?y=1")[0] == 200   # dead first, retried on good
        router.set_healthy(as_backend(good), False)
        router.set_healthy(dead, False)
        assert request(port, "POST", "/p", body=b"z")[0] == 503   # no backend at all
        statuses = [request(port)[0] for _ in range(2)]            # 3rd token, then empty bucket
        assert statuses[-1] == 429
    finally:
        lb.shutdown()
        good.shutdown()
        good.server_close()
    backend, method, path, status, ms, retried, received_at = fake.requests[0]
    assert (backend, method, path, status, retried) == (as_backend(good), "GET", "/x?y=1", 200, True)
    assert ms > 0 and before - 1 < received_at <= time.time()
    assert fake.requests[1][:4] == (None, "POST", "/p", 503)       # NULL server_id in the table
    assert fake.requests[-1][:4] == (None, "GET", "/", 429)


def test_health_checker_logs_tcp_and_http_rows_and_status_changes():
    from lb.config import HealthSettings
    from lb.health import HealthChecker
    from lb.router import Router
    from lb.strategies.round_robin import RoundRobinStrategy
    from tests.test_proxy import as_backend, start_backend

    live = start_backend("live")
    dead = Backend("127.0.0.1", unused_port())
    fake = FakeDB()
    checker = HealthChecker(Router([as_backend(live), dead], RoundRobinStrategy()),
                            HealthSettings(timeout=0.5, unhealthy_threshold=1), db=fake)
    try:
        checker.run_once()
    finally:
        live.shutdown()
        live.server_close()
    kinds = [(b, kind, passed) for b, kind, passed, _ in fake.checks]
    # Live: TCP and HTTP rows. Dead: only a TCP row (HTTP is never attempted).
    assert kinds == [(as_backend(live), "tcp", True), (as_backend(live), "http", True), (dead, "tcp", False)]
    assert all(ms is not None and ms >= 0 for *_, ms in fake.checks)  # refused fast: a real time
    # Each backend's starting status is written on first sight (so a stale
    # 'down'/'removed' row from an earlier run is corrected), then the transition.
    assert fake.statuses == [(as_backend(live), "up"), (dead, "up"), (dead, "down")]


def test_heartbeat_listener_logs_each_accepted_heartbeat():
    from lb.heartbeat import HeartbeatListener
    from lb.router import Router
    from lb.strategies.round_robin import RoundRobinStrategy
    from tests.test_heartbeat import datagram

    fake = FakeDB()
    listener = HeartbeatListener(Router([A], RoundRobinStrategy()), port=0, db=fake)
    listener.handle(datagram(seq=1), 1.0)
    listener.handle(datagram(seq=1), 2.0)    # duplicate: dropped, not logged
    assert fake.metrics == [(A, 12.5, 3.0, 4)]



# --- regressions from the Phase 4 review ------------------------------------------------


@pytest.mark.db
def test_missing_schema_is_one_clear_error_not_a_traceback_per_batch(db_url, caplog):
    """Regression: with the tables missing (init_db.py never run), every batch
    raised UndefinedTable into the "unexpected error" handler: a full traceback
    every 200 ms."""
    import logging
    writer = DBWriter(db_url, DatabaseSettings(batch_size=1, flush_interval_ms=10),
                      connect=lambda u: psycopg.connect(u, autocommit=True, options="-c search_path=no_such_schema"))
    with caplog.at_level(logging.WARNING, logger="lb.db_writer"):
        writer.start()
        for i in range(10):
            writer.submit(request_record(i))
            time.sleep(0.02)
        writer.stop()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "init_db.py" in errors[0].getMessage()
    assert not any(r.exc_info for r in caplog.records)
    assert writer.counters().rejected == 10


def test_stop_before_start_does_not_crash():
    """Regression: stop() joined a thread that was never started (RuntimeError)."""
    assert DBWriter("unused", DatabaseSettings()).stop() is True


def test_logged_latency_excludes_a_slow_client_upload():
    """Regression: the timer started before the request was read, so a client
    taking 1.5 s to send it made a 10 ms backend log 1516 ms."""
    import socket
    from lb.proxy import Proxy
    from lb.router import Router
    from lb.server import LoadBalancer
    from lb.strategies.round_robin import RoundRobinStrategy
    from tests.test_proxy import as_backend, start_backend

    backend = start_backend("b", delay_ms=10)
    fake = FakeDB()
    router = Router([as_backend(backend)], RoundRobinStrategy())
    lb = LoadBalancer(router, "127.0.0.1", 0, proxy=Proxy(router, db=fake))
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
            time.sleep(1.0)
            sent_at = time.time()
            s.sendall(b"\r\n")
            while s.recv(4096):
                pass
    finally:
        lb.shutdown()
        backend.shutdown()
        backend.server_close()
    response_time_ms, received_at = fake.requests[0][4], fake.requests[0][6]
    assert 10 <= response_time_ms < 500
    assert received_at >= sent_at - 0.05  # when the request arrived, not when the client connected
