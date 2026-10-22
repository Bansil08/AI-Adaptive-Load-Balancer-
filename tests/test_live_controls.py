"""Live controls: backend fault injection, admin token, runtime strategy switch,
multi-target heartbeats, and an end-to-end crash through a real LB."""

import http.client
import json
import socket
import threading
import time

import pytest

from backend.agent import HeartbeatAgent, parse_target
from backend.app import BackendServer
from lb.admin import AdminServer
from lb.config import PoolSettings, RetrySettings
from lb.connection_pool import ConnectionPool
from lb.heartbeat import HeartbeatListener
from lb.proxy import Proxy
from lb.router import Router
from lb.server import LoadBalancer
from lb.strategies.base import Backend
from lb.strategies.least_connections import LeastConnectionsStrategy
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_proxy import as_backend, request

TOKEN = "s3cret"


def start_controllable(delay_ms: float = 10, token: str = TOKEN) -> BackendServer:
    server = BackendServer(("127.0.0.1", 0), "x", delay_ms=delay_ms, quiet=True, allow_control=True,
                           control_token=token)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


def post(port, path, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Admin-Token"] = token
    status, _, body = request(port, "POST", path, json.dumps(payload).encode(), headers)
    return status, json.loads(body)


@pytest.fixture
def backend():
    server = start_controllable()
    yield server
    server.shutdown()
    server.server_close()


# --- backend fault injection ---------------------------------------------------------

def test_fault_needs_the_token(backend):
    port = backend.server_address[1]
    assert post(port, "/fault", {"type": "crash"})[0] == 403
    assert post(port, "/fault", {"type": "crash"}, token="wrong")[0] == 403
    assert backend.fault == "none"
    assert post(port, "/control", {"delay_ms": 500}, token="wrong")[0] == 403   # the old endpoint too


def test_fault_disabled_without_allow_control():
    server = BackendServer(("127.0.0.1", 0), "x", quiet=True)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        assert post(server.server_address[1], "/fault", {"type": "crash"})[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_slow_adds_to_the_base_delay_and_restore_undoes_it(backend):
    port = backend.server_address[1]
    status, body = post(port, "/fault", {"type": "slow", "ms": 150}, token=TOKEN)
    assert status == 200 and body["fault"] == "slow" and body["delay_ms"] == 160
    started = time.monotonic()
    assert request(port)[0] == 200
    assert time.monotonic() - started >= 0.15
    assert post(port, "/fault", {"type": "restore"}, token=TOKEN)[1]["delay_ms"] == 10


@pytest.mark.parametrize("payload", [{"type": "explode"}, {"type": "slow", "ms": -5}, {"type": "slow", "ms": "x"}, []])
def test_bad_fault_requests_are_rejected(backend, payload):
    assert post(backend.server_address[1], "/fault", payload, token=TOKEN)[0] == 400


def test_crash_answers_nothing_not_even_health_but_can_be_restored(backend):
    port = backend.server_address[1]
    assert post(port, "/fault", {"type": "crash"}, token=TOKEN)[0] == 200
    for path in ("/", "/health"):
        with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
            request(port, "GET", path)
    status, _, body = request(port, "GET", "/fault")      # the fault endpoint still answers
    assert status == 200 and json.loads(body)["fault"] == "crash"
    assert post(port, "/fault", {"type": "restore"}, token=TOKEN)[1]["fault"] == "none"
    assert request(port, "GET", "/health")[0] == 200


def test_crashed_backend_is_failed_over_by_the_lb(backend):
    """End to end: a crashed backend answers nothing; the LB retries the GET on the
    healthy one, so the client sees no error."""
    healthy = start_controllable()
    router = Router([as_backend(backend), as_backend(healthy)], RoundRobinStrategy())
    proxy = Proxy(router, retry=RetrySettings(max_retries=2, backoff_base=0.01),
                  pool=ConnectionPool(PoolSettings(size=4)))
    lb = LoadBalancer(router, "127.0.0.1", 0, proxy=proxy)
    _, lb_port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        post(backend.server_address[1], "/fault", {"type": "crash"}, token=TOKEN)
        statuses = [request(lb_port)[0] for _ in range(10)]
        assert statuses == [200] * 10
    finally:
        lb.shutdown()
        healthy.shutdown()
        healthy.server_close()


# --- admin API: token and runtime strategy switch ------------------------------------------

A = Backend("127.0.0.1", 9001)


def admin(token=TOKEN):
    factories = {"round_robin": RoundRobinStrategy, "least_connections": LeastConnectionsStrategy}
    return AdminServer(Router([A], RoundRobinStrategy()), token=token, strategies=factories)


def test_admin_changes_need_the_token_reads_do_not():
    server = admin()
    assert server.dispatch("GET", "/backends", b"")[0] == 200
    assert server.dispatch("GET", "/strategy", b"")[0] == 200
    assert server.dispatch("POST", "/strategy", b'{"strategy": "least_connections"}')[0] == 403
    assert server.dispatch("DELETE", "/backends/127.0.0.1:9001", b"", token="nope")[0] == 403
    assert server.router.strategy.name == "round_robin" and server.router.backends == (A,)


def test_admin_without_a_token_configured_allows_changes():
    assert admin(token="").dispatch("POST", "/strategy", b'{"strategy": "least_connections"}')[0] == 200


def test_switching_strategy_at_runtime():
    server = admin()
    status, body = server.dispatch("POST", "/strategy", b'{"strategy": "least_connections"}', TOKEN)
    assert status == 200 and body == {"strategy": "least_connections", "previous": "round_robin"}
    assert server.router.strategy.name == "least_connections"
    # The new strategy is attached to the live stats, so it can route immediately.
    assert server.router.choose(None) == A
    assert server.dispatch("GET", "/strategy", b"")[1] == {"strategy": "least_connections",
                                                           "available": ["least_connections", "round_robin"]}


@pytest.mark.parametrize("body", [b'{"strategy": "magic"}', b"not json", b'{"other": 1}'])
def test_bad_strategy_switch_is_rejected(body):
    server = admin()
    assert server.dispatch("POST", "/strategy", body, TOKEN)[0] == 400
    assert server.router.strategy.name == "round_robin"


# --- heartbeats to several load balancers ---------------------------------------------------

def test_parse_heartbeat_target():
    assert parse_target("lb:9100=backend-fast:9001", "127.0.0.1:9001") == (("lb", 9100), "backend-fast:9001")
    assert parse_target("127.0.0.1:9100", "127.0.0.1:9001") == (("127.0.0.1", 9100), "127.0.0.1:9001")
    with pytest.raises(ValueError):
        parse_target("no-port", "x:1")


def test_agent_reports_to_each_lb_under_its_own_name():
    a, b = Backend("backend-fast", 9001), Backend("127.0.0.1", 9001)
    listeners = [HeartbeatListener(Router([a], RoundRobinStrategy()), port=0),
                 HeartbeatListener(Router([b], RoundRobinStrategy()), port=0)]
    addresses = [l.start() for l in listeners]
    # Plus one target that doesn't resolve (no LB container): must not stop the others.
    agent = HeartbeatAgent([(addresses[0], "backend-fast:9001"), (addresses[1], "127.0.0.1:9001"),
                            (("no-such-host.invalid", 9100), "x:1")], active_connections=lambda: 3, interval=0.05)
    agent.start()
    try:
        deadline = time.monotonic() + 5
        while min(l.accepted for l in listeners) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert all(l.accepted >= 2 for l in listeners)
        assert listeners[0].router.stats.get(a).reported_connections == 3
        assert listeners[1].router.stats.get(b).reported_connections == 3
    finally:
        agent.stop()
        for l in listeners:
            l.stop()


def test_crashed_backend_closes_the_connection_instead_of_hanging():
    # A raw client must see EOF at once (not wait for a timeout): that is what
    # makes the LB's 502 + retry fast.
    server = start_controllable()
    try:
        post(server.server_address[1], "/fault", {"type": "crash"}, token=TOKEN)
        with socket.create_connection(server.server_address, timeout=3) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            assert s.recv(100) == b""
    finally:
        server.shutdown()
        server.server_close()


def test_a_strategy_that_fails_to_build_leaves_routing_unchanged():
    def broken():
        raise RuntimeError("model file corrupt")

    server = AdminServer(Router([A], RoundRobinStrategy()), token=TOKEN, strategies={"ml_latency": broken})
    status, body = server.dispatch("POST", "/strategy", b'{"strategy": "ml_latency"}', TOKEN)
    assert status == 500 and "model file corrupt" in body["error"]
    assert server.router.strategy.name == "round_robin"
