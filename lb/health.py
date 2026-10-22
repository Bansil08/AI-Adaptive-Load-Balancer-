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

from lb.config import HealthSettings
from lb.http_parser import ParseError, SocketReader, read_response
from lb.router import Router
from lb.strategies.base import Backend

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    tcp_ok: bool
    http_ok: bool       # False when TCP failed (not attempted)
    latency_ms: float   # time for the whole probe
    error: str = ""

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

    def elapsed() -> float:
        return (time.perf_counter() - started) * 1000

    try:
        sock = socket.create_connection((backend.host, backend.port), timeout=settings.timeout)
    except OSError as exc:  # refused, unreachable, or timed out (TimeoutError is an OSError)
        return ProbeResult(False, False, elapsed(), f"tcp: {exc}")
    with sock:
        try:
            request = (f"GET {settings.path} HTTP/1.1\r\nHost: {backend}\r\n"
                       "Connection: close\r\nUser-Agent: adaptive-lb-health\r\n\r\n")
            sock.sendall(request.encode("latin-1"))
            # One deadline for the whole exchange, so a backend trickling bytes
            # can't stretch a probe (and the probe round) far past the timeout.
            reader = SocketReader(sock, deadline=time.monotonic() + settings.timeout)
            response = read_response(reader, "GET", max_body=64 * 1024)
        except (OSError, ParseError) as exc:
            return ProbeResult(True, False, elapsed(), f"http: {exc}")
    if response.status_code != 200:
        return ProbeResult(True, False, elapsed(), f"http: status {response.status_code}")
    return ProbeResult(True, True, elapsed())


class HealthChecker:
    """Background thread that probes every backend in the router each round."""

    def __init__(self, router: Router, settings: HealthSettings = HealthSettings()):
        self.router = router
        self.settings = settings
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
            self._states = {
                b: kept if (kept := self._states.get(b)) and kept.healthy == initial[b] else HealthState(initial[b])
                for b in backends
            }
        for backend in backends:
            if self._stop.is_set():
                return
            result = probe(backend, self.settings)
            with self._states_lock:
                state = self._states.get(backend)
                changed = state is not None and state.record(result, self.settings)
                healthy = state.healthy if state else None
            if changed:
                # Logged and applied outside our lock: never call out while holding it.
                self.router.set_healthy(backend, healthy)
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
