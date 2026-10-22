"""Active health checks: a background thread probes every backend on a timer.

Two levels per probe:
  - TCP:  can we complete the 3-way handshake? (Is a process listening?)
  - HTTP: does GET /health return 200 within the timeout? (Is the server
          actually answering, not just accepting connections, e.g. a hung
          process whose kernel still completes handshakes for it?)
A probe passes only if both pass; if TCP fails, HTTP is not attempted.

Hysteresis: one failed probe doesn't mark a backend down and one success
doesn't bring it back. It takes `unhealthy_threshold` (K) consecutive failures
to go down and `healthy_threshold` (M) consecutive successes to come back, so
a single dropped packet doesn't flap a backend in and out of rotation.

Health checks are deliberately NOT pooled: a fresh connection is exactly what
they test. See lb/circuit_breaker.py for how they differ from the breaker.
"""

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lb.config import HealthSettings
from lb.http_parser import ParseError, SocketReader, read_response
from lb.router import Router
from lb.strategies.base import Backend

if TYPE_CHECKING:
    from lb.db_writer import DBWriter

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    tcp_ok: bool
    http_ok: bool       # False when TCP failed (not attempted)
    latency_ms: float   # time for the whole probe
    error: str = ""
    # Per level, for the health_checks table. None = timed out (or, for
    # HTTP, not attempted because TCP failed): there is no time to report.
    tcp_ms: float | None = None
    http_ms: float | None = None

    @property
    def passed(self) -> bool:
        return self.tcp_ok and self.http_ok


class HealthState:
    """Consecutive-result counters for one backend (the K-of / M-of rule)."""

    def __init__(self, healthy: bool = True):
        self.healthy = healthy
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.last: ProbeResult | None = None

    def record(self, result: ProbeResult, settings: HealthSettings) -> bool:
        """Apply one probe result. Returns True if `healthy` changed."""
        self.last = result
        if result.passed:
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            if not self.healthy and self.consecutive_successes >= settings.healthy_threshold:
                self.healthy = True
                return True
        else:
            self.consecutive_failures += 1
            self.consecutive_successes = 0
            if self.healthy and self.consecutive_failures >= settings.unhealthy_threshold:
                self.healthy = False
                return True
        return False


def probe(backend: Backend, settings: HealthSettings) -> ProbeResult:
    """One TCP + HTTP probe over a new connection."""
    started = time.perf_counter()

    def ms_since(t: float) -> float:
        return (time.perf_counter() - t) * 1000

    try:
        sock = socket.create_connection((backend.host, backend.port), timeout=settings.timeout)
    except TimeoutError as exc:
        return ProbeResult(False, False, ms_since(started), f"tcp: {exc or 'timed out'}")
    except OSError as exc:  # refused or unreachable: fails fast, with a real time
        return ProbeResult(False, False, ms_since(started), f"tcp: {exc}", tcp_ms=ms_since(started))
    tcp_ms = ms_since(started)
    http_started = time.perf_counter()
    with sock:
        try:
            request = (f"GET {settings.path} HTTP/1.1\r\nHost: {backend}\r\n"
                       "Connection: close\r\nUser-Agent: adaptive-lb-health\r\n\r\n")
            sock.sendall(request.encode("latin-1"))
            # One deadline for the whole exchange, so a backend trickling bytes
            # can't stretch a probe (and the probe round) far past the timeout.
            reader = SocketReader(sock, deadline=time.monotonic() + settings.timeout)
            response = read_response(reader, "GET", max_body=64 * 1024)
        except TimeoutError as exc:
            return ProbeResult(True, False, ms_since(started), f"http: {exc or 'timed out'}", tcp_ms=tcp_ms)
        except (OSError, ParseError) as exc:
            return ProbeResult(True, False, ms_since(started), f"http: {exc}", tcp_ms=tcp_ms,
                               http_ms=ms_since(http_started))
    http_ms = ms_since(http_started)
    if response.status_code != 200:
        return ProbeResult(True, False, ms_since(started), f"http: status {response.status_code}",
                           tcp_ms=tcp_ms, http_ms=http_ms)
    return ProbeResult(True, True, ms_since(started), tcp_ms=tcp_ms, http_ms=http_ms)


class HealthChecker:
    """Background thread that probes every backend in the router each round."""

    def __init__(self, router: Router, settings: HealthSettings = HealthSettings(),
                 db: "DBWriter | None" = None):
        self.router = router
        self.settings = settings
        self.db = db  # optional: persist every probe into health_checks (Phase 4)
        self._states: dict[Backend, HealthState] = {}
        self._states_lock = threading.Lock()  # admin API reads states while the checker writes
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="health-checker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(self.settings.interval + 2 * self.settings.timeout)

    def run_once(self) -> None:
        """Probe every backend once (the thread calls this every `interval`)."""
        backends = self.router.backends  # immutable snapshot; admin changes show up next round
        # New backends start from the router's view (configured ones up,
        # admin-added ones down). Asked before taking our lock: is_healthy()
        # takes the router's lock, and no code path holds two locks at once.
        initial = {b: self.router.is_healthy(b) for b in backends}
        with self._states_lock:
            # Also forgets backends that were removed. A kept record that
            # disagrees with the router (removed and re-added between rounds,
            # so the router says down while we still say up) starts over;
            # otherwise no transition would ever fire and it would stay down.
            new = [b for b in backends
                   if not ((kept := self._states.get(b)) and kept.healthy == initial[b])]
            self._states = {b: HealthState(initial[b]) if b in new else self._states[b] for b in backends}
        if self.db:
            # servers.status otherwise only changes on a transition, so a row
            # left at 'down' or 'removed' by an earlier run would never be
            # corrected for a backend that starts (optimistically) up.
            for b in new:
                self.db.log_status(b, "up" if initial[b] else "down")
        for backend in backends:
            if self._stop.is_set():
                return
            result = probe(backend, self.settings)
            if self.db:
                self.db.log_health_check(backend, "tcp", result.tcp_ok, result.tcp_ms)
                if result.tcp_ok:  # HTTP is only attempted over a working TCP connection
                    self.db.log_health_check(backend, "http", result.http_ok, result.http_ms)
            with self._states_lock:
                state = self._states.get(backend)
                changed = state is not None and state.record(result, self.settings)
                healthy = state.healthy if state else None
            if changed:
                # Logged and applied outside our lock: never call out while holding it.
                self.router.set_healthy(backend, healthy)
                if self.db:
                    self.db.log_status(backend, "up" if healthy else "down")
                if healthy:
                    log.warning("backend %s is UP (%d consecutive passed checks)",
                                backend, self.settings.healthy_threshold)
                else:
                    log.warning("backend %s is DOWN (%d consecutive failed checks; last: %s)",
                                backend, self.settings.unhealthy_threshold, result.error)

    def describe(self, backend: Backend) -> dict | None:
        """The latest probe and counters, copied under the lock (for the admin API)."""
        with self._states_lock:
            state = self._states.get(backend)
            if state is None:
                return None
            last = state.last
            return {
                "consecutive_failures": state.consecutive_failures,
                "consecutive_successes": state.consecutive_successes,
                "last_tcp_ok": last.tcp_ok if last else None,
                "last_http_ok": last.http_ok if last else None,
                "last_latency_ms": round(last.latency_ms, 1) if last else None,
                "last_error": last.error if last else None,
            }

    def _run(self) -> None:
        # Probe immediately at startup, then every `interval` seconds.
        while True:
            try:
                self.run_once()
            except Exception:
                # A bug in one round must not silently stop all future health checks.
                log.exception("health check round failed")
            if self._stop.wait(self.settings.interval):
                return
