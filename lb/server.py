"""Listener + bounded queue + fixed thread pool (producer-consumer).

The listener thread (producer) accepts TCP connections and puts them into a
BoundedQueue; a fixed pool of worker threads (consumers) takes them out and
serves them. When every worker is busy and the queue is full, the listener
waits, so overload backs up into the kernel's listen backlog instead of
creating unbounded threads or memory (backpressure).

Phase 1's thread-per-connection design is kept behind --thread-per-connection
as the baseline for experiments/benchmark_concurrency.py.

Lock order: no code path in the load balancer holds two locks at once (see
CONCEPTS.md, "Deadlock prevention"). If one ever must, take them in this order
and release in reverse:
    1. BoundedQueue._lock          (lb/bounded_queue.py)
    2. LoadBalancer._handlers_lock (this file)
    3. Router._lock                (lb/router.py)
    4. HealthChecker._states_lock  (lb/health.py)
    5. CircuitBreaker._lock        (lb/circuit_breaker.py)
    6. ServerStats._lock           (lb/stats.py)
    7. ConnectionPool._lock        (lb/connection_pool.py)
    8. RateLimiter._lock           (lb/rate_limiter.py)
    9. strategy locks              (lb/strategies/*)
   10. DBWriter._counters_lock     (lb/db_writer.py; its queue is a BoundedQueue, item 1)
A cycle "A waits for B while B waits for A" needs two threads to take the same
locks in opposite orders; one global order makes that impossible.

Run:  python -m lb.server                      (reads lb.toml)
      python -m lb.server --config other.toml --port 9090
"""

import argparse
import os
import tomllib
import logging
import signal
import socket
import threading
import time

from lb import config
from lb.admin import AdminServer
from lb.bounded_queue import BoundedQueue, QueueClosed
from lb.connection_pool import ConnectionPool
from lb.db_writer import DBWriter
from lb.health import HealthChecker
from lb.heartbeat import HeartbeatListener
from lb.proxy import Proxy, close_gracefully, error_response
from lb.rate_limiter import RateLimiter
from lb.router import Router
from lb.config import ConcurrencySettings, ProxySettings
from lb.strategies import STRATEGIES

log = logging.getLogger(__name__)

# How often the accept loop (and a listener blocked on a full queue) wakes up
# to check whether it should stop.
ACCEPT_POLL_SECONDS = 0.5
# Pause after a failed accept() so we don't spin at 100% CPU while out of file descriptors.
ACCEPT_ERROR_BACKOFF_SECONDS = 0.1


class LoadBalancer:
    def __init__(
        self,
        router: Router,
        host: str = "127.0.0.1",
        port: int = 8080,
        settings: ProxySettings = ProxySettings(),
        concurrency: ConcurrencySettings = ConcurrencySettings(),
        thread_per_connection: bool = False,
        proxy: Proxy | None = None,
    ):
        self.router = router
        self.settings = settings
        # Default: Phase 2 behaviour (no pooling, retries or rate limit).
        self.proxy = proxy or Proxy(router, settings)
        self.concurrency = concurrency
        self.thread_per_connection = thread_per_connection
        self._address = (host, port)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        # Pool mode: accepted (socket, address) pairs waiting for a worker.
        self._queue: BoundedQueue[tuple[socket.socket, tuple]] = BoundedQueue(concurrency.queue_size)
        self._workers: list[threading.Thread] = []
        # Thread-per-connection mode: live handler threads, so shutdown can wait for them.
        self._handlers: set[threading.Thread] = set()
        self._handlers_lock = threading.Lock()

    def start(self) -> tuple[str, int]:
        """Create the listening socket. Returns the bound (host, port); port 0 picks a free one."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # IPv4 + TCP
        # Lets us restart immediately. Otherwise connections from the previous run
        # still in TIME_WAIT make bind() fail with "Address already in use".
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(self._address)
        sock.listen(config.LISTEN_BACKLOG)
        # accept() blocks; a timeout makes it return regularly so the loop can
        # notice the stop flag. (Closing a socket from another thread does not
        # reliably wake a blocked accept() on every OS.)
        sock.settimeout(ACCEPT_POLL_SECONDS)
        self._sock = sock
        return sock.getsockname()[:2]

    def serve_forever(self) -> None:
        """Accept connections until shutdown() is called, then drain and return."""
        if self._sock is None:
            self.start()
        try:
            if not self.thread_per_connection:
                # Workers are daemons only as a last resort: if one is still stuck
                # after shutdown_timeout, it must not keep the process alive.
                # Started inside the try: if the OS refuses thread N ("can't start
                # new thread"), the finally still closes the socket and lets the
                # workers already running exit.
                for i in range(self.concurrency.workers):
                    worker = threading.Thread(target=self._worker, name=f"worker-{i}", daemon=True)
                    worker.start()
                    self._workers.append(worker)
            self._accept_loop()
        finally:
            # Stop accepting first: from here new connections are refused by the
            # kernel, while the ones we already accepted are allowed to finish.
            self._sock.close()
            self._drain()

    def shutdown(self) -> None:
        """Ask serve_forever() to stop accepting, drain and return.

        Only sets a flag, so it is safe to call from a signal handler or any
        thread. (Doing real work in a signal handler can deadlock: the handler
        runs on the main thread, which may already hold the lock it would need.)
        """
        self._stop.set()

    # --- producer: the listener ------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client_sock, client_addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                # e.g. EMFILE "Too many open files" during a connection flood.
                # Uncaught, this would crash the whole LB and drop every
                # in-flight request. Instead only this one client suffers
                # (macOS resets its connection; Linux leaves it queued), and
                # we back off briefly so fds can free up before retrying.
                log.error("accept() failed: %s; backing off", exc)
                time.sleep(ACCEPT_ERROR_BACKOFF_SECONDS)
                continue
            if self.thread_per_connection:
                self._spawn_handler(client_sock, client_addr)
            else:
                self._enqueue(client_sock, client_addr)

    def _enqueue(self, client_sock: socket.socket, client_addr: tuple) -> None:
        # A full queue blocks the listener here: that is the backpressure. The
        # timeout lets it re-check the stop flag instead of waiting forever
        # behind workers that are all stuck on a slow backend.
        while not self._queue.put((client_sock, client_addr), timeout=ACCEPT_POLL_SECONDS):
            if self._stop.is_set():
                # Shutting down with no room left: answer instead of silently
                # closing, so the client knows to retry elsewhere. (Connections
                # still in the kernel backlog, never accept()ed, are reset by the
                # kernel when the listening socket closes; we can't answer those.)
                try:
                    client_sock.sendall(error_response(503))
                except OSError:
                    pass
                close_gracefully(client_sock)
                return

    # --- consumers: the worker pool --------------------------------------------

    def _worker(self) -> None:
        while True:
            try:
                client_sock, client_addr = self._queue.get()
            except QueueClosed:
                return  # shut down and nothing left to serve
            self._serve(client_sock, client_addr)

    def _serve(self, client_sock: socket.socket, client_addr: tuple) -> None:
        try:
            self.proxy.handle_client(client_sock, client_addr)
        except Exception:
            # A bug in one request must not kill the worker: with a fixed pool,
            # every dead worker permanently shrinks capacity.
            log.exception("unexpected error serving %s", client_addr[0])

    # --- baseline: thread-per-connection ---------------------------------------

    def _spawn_handler(self, client_sock: socket.socket, client_addr: tuple) -> None:
        thread = threading.Thread(
            target=self._run_handler,
            args=(client_sock, client_addr),
            name=f"conn-{client_addr[0]}:{client_addr[1]}",
            daemon=True,
        )
        # Registered before start(), so a fast handler can't finish and try to
        # remove itself before it was added.
        with self._handlers_lock:
            self._handlers.add(thread)
        try:
            thread.start()
        except RuntimeError as exc:
            # "can't start new thread": the OS refused another thread.
            # Drop this one connection rather than the whole server.
            log.error("cannot start handler thread: %s; dropping connection", exc)
            with self._handlers_lock:
                self._handlers.discard(thread)
            client_sock.close()

    def _run_handler(self, client_sock: socket.socket, client_addr: tuple) -> None:
        try:
            self._serve(client_sock, client_addr)
        finally:
            with self._handlers_lock:
                self._handlers.discard(threading.current_thread())

    # --- graceful shutdown -------------------------------------------------------

    def _drain(self) -> None:
        """Wait up to shutdown_timeout for accepted connections to be served."""
        deadline = time.monotonic() + self.concurrency.shutdown_timeout
        if self.thread_per_connection:
            with self._handlers_lock:
                threads = list(self._handlers)
        else:
            # Workers finish what is queued, then get() raises QueueClosed.
            self._queue.close()
            threads = self._workers
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

        unserved = self._queue.drain()
        for client_sock, _ in unserved:
            client_sock.close()
        stuck = sum(thread.is_alive() for thread in threads)
        if stuck or unserved:
            log.warning(
                "shutdown timeout (%.1fs): abandoning %d in-flight and %d queued connections",
                self.concurrency.shutdown_timeout, stuck, len(unserved),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Adaptive load balancer")
    parser.add_argument("--config", default=config.DEFAULT_LB_CONFIG,
                        help="TOML config file (default: lb.toml in the repo root)")
    parser.add_argument("--port", type=int, help="override [listen] port from the config file")
    parser.add_argument("--thread-per-connection", action="store_true",
                        help="Phase 1 baseline: one new thread per connection instead of the pool")
    parser.add_argument("--quiet", action="store_true", help="only log warnings and errors")
    parser.add_argument("--run-id", type=int,
                        help="tag every logged request with this experiment_runs id (Phase 5)")
    args = parser.parse_args()

    try:
        cfg = config.load_lb_config(args.config)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        parser.error(f"cannot load config: {exc}")
    if cfg.strategy not in STRATEGIES:
        parser.error(f"unknown strategy {cfg.strategy!r}; choose from {sorted(STRATEGIES)}")

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(threadName)s: %(message)s",
    )
    router = Router(cfg.backends, STRATEGIES[cfg.strategy].from_config(cfg), cfg.breaker, cfg.sticky_sessions)
    pool = ConnectionPool(cfg.pool, cfg.proxy.backend_connect_timeout)
    db = DBWriter(config.database_url(), cfg.database) if cfg.database.enabled else None
    if args.run_id is not None:
        if db is None:
            parser.error("--run-id needs [database] enabled = true")
        db.run_id = args.run_id
    proxy = Proxy(router, cfg.proxy, pool, cfg.retry,
                  RateLimiter(cfg.rate_limit) if cfg.rate_limit.enabled else None, db)
    lb = LoadBalancer(router, cfg.host, args.port or cfg.port, cfg.proxy, cfg.concurrency,
                      thread_per_connection=args.thread_per_connection, proxy=proxy)
    health = HealthChecker(router, cfg.health, db)
    heartbeat = (HeartbeatListener(router, cfg.heartbeat.host, cfg.heartbeat.port, db)
                 if cfg.heartbeat.port else None)
    # Any strategy can be switched to at runtime (POST /strategy), built from this config.
    strategy_factories = {name: (lambda cls=cls: cls.from_config(cfg)) for name, cls in STRATEGIES.items()}
    admin = (AdminServer(router, cfg.admin, health, pool, db, token=os.environ.get("ADMIN_TOKEN", ""),
                         strategies=strategy_factories)
             if cfg.admin.port else None)

    def on_signal(signum, frame):
        lb.shutdown()
        # A second signal of either kind (e.g. SIGTERM, then Ctrl+C) now gets the
        # OS default action: kill immediately, for when draining takes too long.
        # (No logging in here: the logging module takes a lock the interrupted
        # main thread may hold.)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # Python runs signal handlers on the main thread, which is the listener.
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        host, port = lb.start()
        if db:
            # Connects lazily in its own thread: a database that is down slows
            # nothing here; records wait in the queue until it is reachable.
            db.start()
            log.info("request log: PostgreSQL, batches of %d or every %d ms",
                     cfg.database.batch_size, cfg.database.flush_interval_ms)
        health.start()
        if heartbeat:
            log.info("heartbeats: UDP %s:%d", *heartbeat.start())
        if admin:
            log.info("admin API: http://%s:%d/backends", *admin.start())
    except OSError as exc:  # e.g. a port already in use
        parser.error(f"cannot start: {exc}")
    mode = "thread-per-connection" if args.thread_per_connection else (
        f"pool of {cfg.concurrency.workers} workers, queue {cfg.concurrency.queue_size}")
    log.info("listening on %s:%d, strategy=%s%s, %s, backends=%s",
             host, port, cfg.strategy, " (sticky)" if cfg.sticky_sessions else "", mode,
             ", ".join(map(str, cfg.backends)) or "(none)")
    try:
        lb.serve_forever()
    finally:
        # After the drain: in-flight requests may still have needed the pool.
        for component in (admin, heartbeat, health):
            if component:
                component.stop()
        pool.close_all()
        if db:
            # Last, after every producer has stopped, so nothing is queued
            # after the final flush.
            db.stop()
    log.info("stopped")


if __name__ == "__main__":
    main()
