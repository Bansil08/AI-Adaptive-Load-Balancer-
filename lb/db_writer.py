"""Single DB writer thread that inserts queued log records in batched transactions.

Why one writer with batching instead of every worker inserting its own row:
  - Latency: an INSERT + COMMIT is a network round trip plus an fsync of the
    write-ahead log. Done by the worker, every client would wait for the
    disk. Here a worker only does a non-blocking put() onto a queue.
  - Throughput: one transaction of 200 rows costs one commit (one fsync)
    instead of 200, and executemany() sends the rows without waiting for
    each reply (see experiments/results/insert_benchmark.md).
  - Connections: one connection instead of one per worker (each Postgres
    connection is a whole server process).
  - Isolation from failures: if the database is slow or down, workers keep
    serving; the queue absorbs bursts and, when full, drops records (counted)
    rather than blocking a client.

Batching: the thread blocks for a first record, then keeps collecting until
the batch has `batch_size` records (size trigger) or `flush_interval_ms` has
passed since that first record (time trigger), whichever comes first, and
writes the batch in ONE transaction: all of it commits or none of it does.

Failures:
  - Bad data (a CHECK or foreign-key violation): Postgres aborts the whole
    transaction, so the batch is rolled back. It is then retried row by row,
    each row inside a SAVEPOINT, so the good rows are saved and only the bad
    one is dropped. The writer thread carries on.
  - Lost connection (database restarted or unreachable): reconnect with
    backoff and retry the SAME batch, so nothing is lost; during shutdown it
    gives up when the shutdown timeout runs out. (If the connection dies
    after the COMMIT reached the server but before its reply reached us, the
    retry inserts the batch twice: delivery is "at least once". Acceptable
    for logs; exactly-once would need an idempotency key per row.)

Records carry the time they were created (when the request arrived), not the
time the batch was written, so batching never skews timestamps.

The queue is lb/bounded_queue.py from Phase 2: the writer is just another
consumer. Only this thread touches the database connection.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import psycopg

from lb.bounded_queue import BoundedQueue, QueueClosed
from lb.config import DatabaseSettings
from lb.strategies.base import Backend

log = logging.getLogger(__name__)

RECONNECT_BACKOFF_START = 0.5   # seconds; doubles after each failed attempt
RECONNECT_BACKOFF_MAX = 5.0
SCHEMA_ERROR_LOG_EVERY = 60.0   # seconds between repeats of the "schema missing" error


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


@dataclass(frozen=True)
class RequestRecord:
    received_at: float           # time.time() when the request arrived
    backend: Backend | None      # None: no backend served it (429, 503)
    method: str
    path: str
    status_code: int
    response_time_ms: float
    retried: bool
    run_id: int | None = None    # set during experiments (Phase 5)


@dataclass(frozen=True)
class HealthCheckRecord:
    checked_at: float
    backend: Backend
    check_type: str              # 'tcp' or 'http'
    passed: bool
    latency_ms: float | None     # None when the probe timed out


@dataclass(frozen=True)
class MetricRecord:
    recorded_at: float
    backend: Backend
    cpu_percent: float
    memory_percent: float
    active_connections: int


@dataclass(frozen=True)
class StatusRecord:
    backend: Backend
    status: str                  # 'up', 'down' or 'removed'


Record = RequestRecord | HealthCheckRecord | MetricRecord | StatusRecord

INSERT_REQUEST = (
    "INSERT INTO requests (run_id, server_id, received_at, method, path, status_code,"
    " response_time_ms, retried) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)
INSERT_HEALTH_CHECK = (
    "INSERT INTO health_checks (server_id, checked_at, check_type, passed, latency_ms)"
    " VALUES (%s, %s, %s, %s, %s)"
)
INSERT_METRIC = (
    "INSERT INTO server_metrics (server_id, recorded_at, cpu_percent, memory_percent,"
    " active_connections) VALUES (%s, %s, %s, %s, %s)"
)
UPDATE_STATUS = "UPDATE servers SET status = %s WHERE id = %s"
# DO UPDATE (a no-op change) rather than DO NOTHING, because DO NOTHING returns
# no row when the server already exists, and we need its id either way.
UPSERT_SERVER = (
    "INSERT INTO servers (address, port) VALUES (%s, %s)"
    " ON CONFLICT (address, port) DO UPDATE SET address = EXCLUDED.address RETURNING id"
)


@dataclass(frozen=True)
class WriterCounters:
    queued: int          # records accepted onto the queue
    dropped: int         # records refused because the queue was full (or the writer stopped)
    written: int         # rows committed to the database
    rejected: int        # rows the database refused (dropped after the row-by-row retry)
    batches: int         # batches committed
    failed_batches: int  # batches that had to be rolled back
    reconnects: int      # times the connection was lost and re-opened


class DBWriter:
    def __init__(self, conninfo: str, settings: DatabaseSettings = DatabaseSettings(),
                 connect: Callable[[str], psycopg.Connection] | None = None):
        self.settings = settings
        self._conninfo = conninfo
        # autocommit=True: nothing is in a transaction unless we open one with
        # `with conn.transaction()`, so those blocks are the only transaction
        # boundaries and no transaction is ever left open by accident.
        self._connect = connect or (lambda info: psycopg.connect(info, connect_timeout=3, autocommit=True))
        self._ever_connected = False
        self._queue: BoundedQueue[Record] = BoundedQueue(settings.queue_size)
        self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)
        self._conn: psycopg.Connection | None = None
        self._server_ids: dict[Backend, int] = {}  # only the writer thread touches this
        self._give_up_at: float | None = None      # set by stop(): when retrying must end
        self._counters_lock = threading.Lock()
        self._c = {name: 0 for name in WriterCounters.__dataclass_fields__}
        self.run_id: int | None = None  # tagged onto requests during an experiment (Phase 5)
        self._schema_error_logged_at = -SCHEMA_ERROR_LOG_EVERY

    # --- producer side: called from worker / health / heartbeat threads -------------

    def submit(self, record: Record) -> bool:
        """Queue a record without ever blocking. False if it had to be dropped."""
        try:
            accepted = self._queue.put(record, timeout=0)  # timeout=0: never wait
        except QueueClosed:
            accepted = False
        self._count("queued" if accepted else "dropped")
        return accepted

    def log_request(self, backend: Backend | None, method: str, path: str, status_code: int,
                    response_time_ms: float, retried: bool, received_at: float) -> bool:
        return self.submit(RequestRecord(received_at, backend, method, path, status_code,
                                         response_time_ms, retried, self.run_id))

    def log_health_check(self, backend: Backend, check_type: str, passed: bool,
                         latency_ms: float | None) -> bool:
        return self.submit(HealthCheckRecord(time.time(), backend, check_type, passed, latency_ms))

    def log_metric(self, backend: Backend, cpu_percent: float, memory_percent: float,
                   active_connections: int) -> bool:
        return self.submit(MetricRecord(time.time(), backend, cpu_percent, memory_percent,
                                        active_connections))

    def log_status(self, backend: Backend, status: str) -> bool:
        return self.submit(StatusRecord(backend, status))

    # --- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> bool:
        """Flush everything still queued (up to shutdown_timeout). True if all was written."""
        if self._thread.ident is None:  # never started: nothing was written, nothing to flush
            self._queue.close()
            left = len(self._queue)
            return left == 0
        dropped_before = self.counters().dropped
        self._give_up_at = time.monotonic() + self.settings.shutdown_timeout
        # close() lets the thread drain what is queued: get() keeps returning
        # items until the queue is empty, then raises QueueClosed.
        self._queue.close()
        self._thread.join(self.settings.shutdown_timeout + 1)
        left = len(self._queue)
        dropped = self.counters().dropped - dropped_before
        if self._thread.is_alive() or left or dropped:
            log.warning("db writer: shutdown: %d records not written", left + dropped)
            return False
        return True

    def counters(self) -> WriterCounters:
        with self._counters_lock:
            return WriterCounters(**self._c)

    # --- consumer side: the writer thread ---------------------------------------------

    def _run(self) -> None:
        while True:
            batch = self._next_batch()
            if batch is None:
                break
            try:
                self._write_with_retry(batch)
            except Exception:
                # A bug must not kill the only writer: log, drop this batch, continue.
                log.exception("db writer: unexpected error, dropping %d records", len(batch))
                self._count("rejected", len(batch))
        if self._conn is not None:
            self._conn.close()

    def _next_batch(self) -> list[Record] | None:
        """Block for one record, then collect until the size or the time trigger fires."""
        try:
            first = self._queue.get()
        except QueueClosed:
            return None  # stopped and fully drained
        batch = [first]
        deadline = time.monotonic() + self.settings.flush_interval_ms / 1000
        while len(batch) < self.settings.batch_size:                 # size trigger
            remaining = deadline - time.monotonic()
            if remaining <= 0:                                        # time trigger
                break
            try:
                batch.append(self._queue.get(timeout=remaining))
            except (TimeoutError, QueueClosed):
                break
        return batch

    def _write_with_retry(self, batch: list[Record]) -> None:
        backoff = RECONNECT_BACKOFF_START
        while True:
            try:
                conn = self._connection()
                try:
                    self._write_batch(conn, batch)
                except (psycopg.DataError, psycopg.IntegrityError) as exc:
                    # The transaction was rolled back as a whole; save what we can.
                    self._count("failed_batches")
                    log.warning("db writer: batch of %d rolled back (%s); retrying row by row",
                                len(batch), exc.diag.message_primary or exc)
                    self._write_rows_individually(conn, batch)
                return
            except psycopg.ProgrammingError as exc:
                # e.g. UndefinedTable: the schema was never applied. Retrying
                # can't help and a traceback per batch (every 200 ms) would bury
                # the message, so say it clearly, at most once a minute.
                self._count("rejected", len(batch))
                now = time.monotonic()
                if now - self._schema_error_logged_at >= SCHEMA_ERROR_LOG_EVERY:
                    self._schema_error_logged_at = now
                    log.error("db writer: %s; dropping records (did you run scripts/init_db.py?)",
                              exc.diag.message_primary or exc)
                return
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                # Connection-level problem: the batch is intact in memory, so retry it.
                self._drop_connection()
                if self._give_up_at is not None and time.monotonic() + backoff > self._give_up_at:
                    log.error("db writer: database unavailable at shutdown, dropping %d records", len(batch))
                    self._count("dropped", len(batch))
                    return
                log.warning("db writer: database unavailable (%s); retrying in %.1fs", exc, backoff)
                time.sleep(backoff)
                backoff = min(RECONNECT_BACKOFF_MAX, backoff * 2)

    def _write_batch(self, conn: psycopg.Connection, batch: list[Record]) -> None:
        ids = self._resolve_servers(conn, batch)
        requests, checks, metrics, statuses = self._rows(batch, ids)
        # One transaction for the whole batch: `with conn.transaction()` commits
        # at the end of the block, or rolls back if anything inside raises.
        with conn.transaction(), conn.cursor() as cur:
            if requests:
                cur.executemany(INSERT_REQUEST, requests)
            if checks:
                cur.executemany(INSERT_HEALTH_CHECK, checks)
            if metrics:
                cur.executemany(INSERT_METRIC, metrics)
            for status, server_id in statuses:
                cur.execute(UPDATE_STATUS, (status, server_id))
        self._count("written", len(batch))
        self._count("batches")

    def _write_rows_individually(self, conn: psycopg.Connection, batch: list[Record]) -> None:
        """Retry a failed batch one row at a time; only the rows the database refuses are lost."""
        ids = self._resolve_servers(conn, batch)
        written = 0
        with conn.transaction():
            for record in batch:
                requests, checks, metrics, statuses = self._rows([record], ids)
                try:
                    # A nested transaction() is a SAVEPOINT: a failure rolls back
                    # to it, undoing this row only, and the outer transaction lives on.
                    with conn.transaction(), conn.cursor() as cur:
                        for sql, rows in ((INSERT_REQUEST, requests), (INSERT_HEALTH_CHECK, checks),
                                          (INSERT_METRIC, metrics)):
                            for row in rows:
                                cur.execute(sql, row)
                        for status, server_id in statuses:
                            cur.execute(UPDATE_STATUS, (status, server_id))
                    written += 1
                except (psycopg.DataError, psycopg.IntegrityError) as exc:
                    log.warning("db writer: rejected %r: %s", record, exc.diag.message_primary or exc)
                    self._count("rejected")
        self._count("written", written)

    def _resolve_servers(self, conn: psycopg.Connection, batch: list[Record]) -> dict[Backend, int]:
        """servers.id for every backend in the batch, creating rows for new ones."""
        missing = {r.backend for r in batch if r.backend is not None} - self._server_ids.keys()
        if missing:
            # Its own transaction, committed BEFORE the batch: if the batch then
            # rolls back, the cached ids must still exist in the table.
            with conn.transaction(), conn.cursor() as cur:
                found = {}
                for backend in sorted(missing, key=str):
                    cur.execute(UPSERT_SERVER, (backend.host, backend.port))
                    found[backend] = cur.fetchone()[0]
            self._server_ids.update(found)
        return self._server_ids

    @staticmethod
    def _rows(batch: list[Record], ids: dict[Backend, int]):
        requests, checks, metrics, statuses = [], [], [], []
        for r in batch:
            if isinstance(r, RequestRecord):
                requests.append((r.run_id, ids[r.backend] if r.backend else None, _utc(r.received_at),
                                 r.method, r.path, r.status_code, r.response_time_ms, r.retried))
            elif isinstance(r, HealthCheckRecord):
                checks.append((ids[r.backend], _utc(r.checked_at), r.check_type, r.passed, r.latency_ms))
            elif isinstance(r, MetricRecord):
                metrics.append((ids[r.backend], _utc(r.recorded_at), r.cpu_percent, r.memory_percent,
                                r.active_connections))
            else:
                statuses.append((r.status, ids[r.backend]))
        return requests, checks, metrics, statuses

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect(self._conninfo)
            if self._ever_connected:
                self._count("reconnects")
            self._ever_connected = True
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None

    def _count(self, name: str, amount: int = 1) -> None:
        with self._counters_lock:
            self._c[name] += amount
