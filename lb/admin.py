"""Admin HTTP API on its own port, to inspect and change backends at runtime.

  GET    /backends                  active strategy, backends (health, circuit state, stats),
                                    pool and DB writer counters (the dashboard polls this)
  POST   /backends                  add one; body {"address": "127.0.0.1:9004"}
  DELETE /backends/127.0.0.1:9004   drain (wait for its in-flight requests), then remove
  GET    /strategy                  the active strategy and the ones available
  POST   /strategy                  switch at runtime; body {"strategy": "ml_latency"}

Requests that change something (POST, DELETE) must carry the header
"X-Admin-Token: <token>" when a token is configured (the ADMIN_TOKEN
environment variable; lb/server.py passes it in). Reads stay open.

A separate port keeps admin traffic out of the client path and lets it be
firewalled separately. It binds to loopback by default; when it has to listen
more widely (the LB in a Docker container, reached by the dashboard container),
set a token, or anyone who can reach it could remove every backend.

Each admin connection gets its own short-lived thread rather than a pool
worker: admin traffic is tiny, and a DELETE can block for up to drain_timeout
while requests finish, which must not stall client traffic or other admin calls.

A backend added here starts DOWN and joins once it passes `healthy_threshold`
health checks in a row, so traffic never goes to a server that isn't ready.
"""

import hmac
import json
import logging
import socket
import threading
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Callable

from lb.config import AdminSettings
from lb.connection_pool import ConnectionPool
from lb.health import HealthChecker
from lb.http_parser import Headers, HTTPResponse, ParseError, SocketReader, read_request
from lb.proxy import close_gracefully
from lb.router import Router
from lb.strategies.base import Backend, LoadBalancingStrategy

if TYPE_CHECKING:
    from lb.db_writer import DBWriter

log = logging.getLogger(__name__)

POLL_SECONDS = 0.5
CLIENT_TIMEOUT = 5.0
MAX_BODY = 4096


def json_response(status: int, payload) -> bytes:
    body = json.dumps(payload, indent=2).encode() + b"\n"
    headers = Headers([
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Connection", "close"),
    ])
    return HTTPResponse("HTTP/1.1", status, HTTPStatus(status).phrase, headers, body).to_bytes()


class AdminServer:
    def __init__(self, router: Router, settings: AdminSettings = AdminSettings(),
                 health: HealthChecker | None = None, pool: ConnectionPool | None = None,
                 db: "DBWriter | None" = None, token: str = "",
                 strategies: dict[str, Callable[[], LoadBalancingStrategy]] | None = None):
        self.router = router
        self.token = token
        # Name -> function building that strategy from the LB's config (lb/server.py).
        self.strategies = strategies or {}
        self.db = db
        self.settings = settings
        self.health = health
        self.pool = pool
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, name="admin", daemon=True)

    def start(self) -> tuple[str, int]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.settings.host, self.settings.port))
        sock.listen(16)
        sock.settimeout(POLL_SECONDS)
        self._sock = sock
        self._thread.start()
        return sock.getsockname()[:2]

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2 * POLL_SECONDS + 1)
        if self._sock is not None:
            self._sock.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return
                log.error("admin accept() failed: %s", exc)
                time.sleep(0.1)
                continue
            threading.Thread(target=self._serve, args=(conn,), name="admin-conn", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(CLIENT_TIMEOUT)
            try:
                request = read_request(SocketReader(conn, deadline=time.monotonic() + CLIENT_TIMEOUT),
                                       max_body=MAX_BODY)
            except ParseError as exc:
                conn.sendall(json_response(exc.status_code, {"error": exc.message}))
                return
            if request is None:
                return
            status, payload = self.dispatch(request.method, request.target, request.body,
                                            request.headers.get("x-admin-token") or "")
            conn.sendall(json_response(status, payload))
        except OSError as exc:
            log.debug("admin connection error: %s", exc)
        except Exception:
            log.exception("admin request failed")
        finally:
            close_gracefully(conn)

    # --- routing of admin requests (pure: easy to unit-test without sockets) ---------

    def dispatch(self, method: str, target: str, body: bytes, token: str = "") -> tuple[int, dict]:
        path = target.split("?", 1)[0].rstrip("/")
        # compare_digest: constant time, so the token can't be guessed character by character.
        if method not in ("GET", "HEAD") and self.token and not hmac.compare_digest(token.encode(),
                                                                                    self.token.encode()):
            return 403, {"error": "missing or wrong X-Admin-Token"}
        if path == "/strategy":
            if method == "GET":
                return 200, {"strategy": self.router.strategy.name, "available": sorted(self.strategies)}
            if method == "POST":
                return self.set_strategy(body)
            return 405, {"error": "use GET or POST on /strategy"}
        if path == "/backends":
            if method == "GET":
                strategy = self.router.strategy
                payload = {"strategy": strategy.name, "sticky_sessions": self.router.sticky_sessions,
                           "backends": self.list_backends()}
                if hasattr(strategy, "fallbacks"):  # the ML strategies count their fallbacks
                    payload["fallbacks"] = strategy.fallbacks
                if self.pool:
                    payload["pool"] = vars(self.pool.counters())
                if self.db:
                    payload["db_writer"] = vars(self.db.counters())
                return 200, payload
            if method == "POST":
                return self.add_backend(body)
            return 405, {"error": "use GET or POST on /backends"}
        if path.startswith("/backends/"):
            if method == "DELETE":
                return self.remove_backend(path[len("/backends/"):])
            return 405, {"error": "use DELETE on /backends/<host:port>"}
        return 404, {"error": "not found; try GET /backends or GET /strategy"}

    def set_strategy(self, body: bytes) -> tuple[int, dict]:
        try:
            name = json.loads(body or b"{}")["strategy"]
        except (ValueError, KeyError, TypeError):
            return 400, {"error": 'body must be JSON like {"strategy": "least_connections"}'}
        if name not in self.strategies:
            return 400, {"error": f"unknown strategy {name!r}; choose from {sorted(self.strategies)}"}
        previous = self.router.strategy.name
        # Built (the ML model loaded from disk) BEFORE the switch, on this admin
        # thread, so routing never waits for it; if building fails, nothing changes.
        try:
            strategy = self.strategies[name]()
        except Exception as exc:
            log.exception("admin: cannot build strategy %s", name)
            return 500, {"error": f"cannot build {name}: {exc}"}
        self.router.set_strategy(strategy)
        log.warning("admin: strategy %s -> %s", previous, name)
        return 200, {"strategy": name, "previous": previous}

    def list_backends(self) -> list[dict]:
        stats = self.router.stats.snapshot()
        now = time.monotonic()
        rows = []
        for view in self.router.views():
            s = stats.get(view.backend)
            row = {
                "address": str(view.backend),
                "healthy": view.healthy,
                "draining": view.draining,
                "circuit": view.breaker_state,
                "in_flight": view.in_flight,
                "total_requests": s.total_requests if s else 0,
                "avg_latency_ms": round(s.avg_latency_ms, 1) if s and s.avg_latency_ms is not None else None,
            }
            if s and s.heartbeat_at is not None:
                row["heartbeat"] = {
                    "cpu_percent": s.cpu_percent,
                    "memory_percent": s.memory_percent,
                    "active_connections": s.reported_connections,
                    "age_s": round(now - s.heartbeat_at, 1),
                }
            health = self.health.describe(view.backend) if self.health else None
            if health:
                row["health_check"] = health
            rows.append(row)
        return rows

    def add_backend(self, body: bytes) -> tuple[int, dict]:
        try:
            payload = json.loads(body or b"{}")
            backend = Backend.parse(str(payload["address"]))
        except (ValueError, KeyError, TypeError):
            return 400, {"error": 'body must be JSON like {"address": "127.0.0.1:9004"}'}
        # Starts down; the health checker brings it up after M passed checks.
        if not self.router.add_backend(backend, healthy=self.health is None):
            return 409, {"error": f"{backend} is already a backend"}
        # (Its servers.status row is written by the health checker on first sight.)
        log.warning("admin: added backend %s", backend)
        return 201, {"added": str(backend), "healthy": self.router.is_healthy(backend)}

    def remove_backend(self, address: str) -> tuple[int, dict]:
        try:
            backend = Backend.parse(address)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        if backend not in self.router.backends:
            return 404, {"error": f"{backend} is not a backend"}
        started = time.monotonic()
        result = self.router.drain_and_remove(backend, self.settings.drain_timeout)
        if result is None:
            return 409, {"error": f"{backend} is already being removed"}
        drained, left = result
        if self.pool:
            self.pool.discard_backend(backend)
        if self.db:
            self.db.log_status(backend, "removed")  # soft delete: its history keeps a valid server_id
        # Stats are kept: a request still in flight after a drain timeout
        # will decrement its counters later, and deleting the entry would let
        # that recreate it at -1 active connections.
        waited = round(time.monotonic() - started, 2)
        if drained:
            log.warning("admin: removed backend %s after draining for %.2fs", backend, waited)
        else:
            log.warning("admin: removed backend %s with %d requests still in flight (drain timeout)",
                        backend, left)
        return 200, {"removed": str(backend), "drained": drained, "waited_s": waited,
                     "in_flight_at_removal": left}
