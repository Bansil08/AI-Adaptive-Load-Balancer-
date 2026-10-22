"""Health checks: K-down / M-up transitions, TCP vs HTTP probes, and the router honouring them."""

import socket
import threading
import time

import pytest

from backend.app import BackendServer
from lb.config import HealthSettings
from lb.health import HealthChecker, HealthState, ProbeResult, probe
from lb.router import NoBackendAvailable, Router
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_proxy import as_backend, start_backend, unused_port
from tests.test_round_robin import REQUEST

SETTINGS = HealthSettings(interval=0.1, timeout=0.3, unhealthy_threshold=3, healthy_threshold=2)
PASS = ProbeResult(True, True, 1.0)
FAIL = ProbeResult(False, False, 1.0, "refused")


def test_marked_down_only_after_k_consecutive_failures():
    state = HealthState(healthy=True)
    assert not state.record(FAIL, SETTINGS)
    assert not state.record(FAIL, SETTINGS)
    assert state.healthy
    assert state.record(FAIL, SETTINGS)  # the 3rd in a row changes the state
    assert not state.healthy


def test_a_pass_resets_the_failure_streak():
    state = HealthState(healthy=True)
    for result in (FAIL, FAIL, PASS, FAIL, FAIL):
        state.record(result, SETTINGS)
    assert state.healthy


def test_marked_up_only_after_m_consecutive_successes():
    state = HealthState(healthy=False)
    assert not state.record(PASS, SETTINGS)
    assert not state.healthy
    assert state.record(PASS, SETTINGS)
    assert state.healthy


def test_a_failure_resets_the_success_streak():
    state = HealthState(healthy=False)
    for result in (PASS, FAIL, PASS):
        state.record(result, SETTINGS)
    assert not state.healthy


def test_http_failure_with_tcp_ok_counts_as_failed():
    result = ProbeResult(tcp_ok=True, http_ok=False, latency_ms=1.0)
    assert not result.passed


# --- probes against real sockets ------------------------------------------------


@pytest.fixture
def backend():
    server = start_backend("a")
    yield server
    server.shutdown()
    server.server_close()


def test_probe_passes_for_a_live_backend(backend):
    result = probe(as_backend(backend), SETTINGS)
    assert result.tcp_ok and result.http_ok and result.passed


def test_probe_fails_at_tcp_level_when_nothing_listens():
    result = probe(Backend("127.0.0.1", unused_port()), SETTINGS)
    assert (result.tcp_ok, result.http_ok) == (False, False)
    assert result.error.startswith("tcp:")


def silent_server():
    """Accepts TCP connections (the kernel does the handshake) but never answers:
    a hung process. Only the HTTP level can tell it from a healthy one."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock


def test_probe_fails_at_http_level_when_the_server_accepts_but_never_answers():
    sock = silent_server()
    try:
        started = time.monotonic()
        result = probe(Backend("127.0.0.1", sock.getsockname()[1]), SETTINGS)
        assert (result.tcp_ok, result.http_ok) == (True, False)
        assert time.monotonic() - started < SETTINGS.timeout + 0.5
    finally:
        sock.close()


def test_probe_fails_when_health_returns_non_200():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)

    def answer_503():
        conn, _ = sock.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")

    threading.Thread(target=answer_503, daemon=True).start()
    try:
        result = probe(Backend("127.0.0.1", sock.getsockname()[1]), SETTINGS)
        assert (result.tcp_ok, result.http_ok) == (True, False)
        assert "503" in result.error
    finally:
        sock.close()


def test_checker_takes_a_dead_backend_out_and_puts_it_back(backend):
    live = as_backend(backend)
    port = unused_port()
    flaky = Backend("127.0.0.1", port)
    router = Router([live, flaky], RoundRobinStrategy())
    checker = HealthChecker(router, SETTINGS)

    for _ in range(SETTINGS.unhealthy_threshold):
        checker.run_once()
    assert not router.is_healthy(flaky) and router.is_healthy(live)
    assert {router.choose(REQUEST) for _ in range(6)} == {live}

    # Bring the "flaky" backend up on its port: it rejoins after M passing rounds.
    revived = BackendServer(("127.0.0.1", port), "revived", quiet=True)
    threading.Thread(target=revived.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        checker.run_once()
        assert not router.is_healthy(flaky)  # one pass is not enough
        checker.run_once()
        assert router.is_healthy(flaky)
        assert {router.choose(REQUEST) for _ in range(6)} == {live, flaky}
    finally:
        revived.shutdown()
        revived.server_close()


def test_no_healthy_backend_means_503_material():
    router = Router([Backend("127.0.0.1", unused_port())], RoundRobinStrategy())
    checker = HealthChecker(router, HealthSettings(unhealthy_threshold=1, timeout=0.3))
    checker.run_once()
    with pytest.raises(NoBackendAvailable, match="no healthy backend"):
        router.acquire(REQUEST)


def test_background_thread_runs_rounds_until_stopped():
    router = Router([Backend("127.0.0.1", unused_port())], RoundRobinStrategy())
    checker = HealthChecker(router, HealthSettings(interval=0.05, timeout=0.2, unhealthy_threshold=2))
    checker.start()
    try:
        deadline = time.monotonic() + 3
        while router.is_healthy(router.backends[0]) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not router.is_healthy(router.backends[0])
    finally:
        checker.stop()
    assert not checker._thread.is_alive()


def test_backend_removed_and_re_added_comes_back_up(backend):
    """Regression: the checker kept its old "healthy" record for the address,
    the router said "down" (admin-added), and since the record never changed
    state no UP transition fired: the backend stayed out of rotation forever."""
    b = as_backend(backend)
    router = Router([b], RoundRobinStrategy())
    checker = HealthChecker(router, SETTINGS)
    checker.run_once()
    router.drain_and_remove(b, timeout=1)
    router.add_backend(b, healthy=False)
    for _ in range(SETTINGS.healthy_threshold):
        checker.run_once()
    assert router.is_healthy(b)
