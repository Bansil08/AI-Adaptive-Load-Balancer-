"""End-to-end tests: real backends + real load balancer over localhost TCP."""

import http.client
import json
import socket
import threading

import pytest

from backend.app import BackendServer
from lb import config
from lb.router import Router
from lb.server import LoadBalancer
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy


def start_backend(name: str, delay_ms: float = 0) -> BackendServer:
    server = BackendServer(("127.0.0.1", 0), name, delay_ms=delay_ms, quiet=True)
    # Short poll interval so shutdown() in teardown returns quickly.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


def start_lb(backends: list[Backend]) -> tuple[LoadBalancer, int]:
    lb = LoadBalancer(Router(backends, RoundRobinStrategy()), "127.0.0.1", 0)
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    return lb, port


def as_backend(server: BackendServer) -> Backend:
    return Backend("127.0.0.1", server.server_address[1])


def unused_port() -> int:
    """A port with nothing listening on it (connections are refused)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def cluster():
    """Three backends a, b, c behind one load balancer. Yields the LB port."""
    servers = [start_backend(name) for name in ("a", "b", "c")]
    lb, port = start_lb([as_backend(s) for s in servers])
    yield port
    lb.shutdown()
    for s in servers:
        s.shutdown()
        s.server_close()


def request(port, method="GET", path="/", body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def raw_request(port, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(data)
        chunks = []
        while chunk := s.recv(4096):
            chunks.append(chunk)
        return b"".join(chunks)


def test_round_robin_across_real_backends(cluster):
    served_by = [json.loads(request(cluster)[2])["server"] for _ in range(6)]
    assert served_by == ["a", "b", "c", "a", "b", "c"]


def test_adds_x_forwarded_for(cluster):
    _, _, body = request(cluster)
    assert json.loads(body)["x_forwarded_for"] == "127.0.0.1"


def test_appends_to_existing_x_forwarded_for(cluster):
    _, _, body = request(cluster, headers={"X-Forwarded-For": "203.0.113.7"})
    assert json.loads(body)["x_forwarded_for"] == "203.0.113.7, 127.0.0.1"


def test_post_body_is_forwarded(cluster):
    status, _, body = request(cluster, "POST", "/submit", body=b"x" * 5000)
    assert status == 200
    assert json.loads(body)["body_bytes"] == 5000


def test_head_request_gets_headers_but_no_body(cluster):
    status, headers, body = request(cluster, "HEAD", "/")
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0


def test_response_headers_relayed_and_connection_closed(cluster):
    _, headers, _ = request(cluster)
    assert headers["X-Backend"] in {"a", "b", "c"}
    assert headers["Connection"] == "close"


def test_health_endpoint_through_lb(cluster):
    status, _, body = request(cluster, path="/health")
    assert status == 200 and json.loads(body)["status"] == "ok"


def test_malformed_request_gets_400(cluster):
    assert raw_request(cluster, b"NONSENSE\r\n\r\n").startswith(b"HTTP/1.1 400 ")


def test_unsupported_version_gets_505(cluster):
    assert raw_request(cluster, b"GET / HTTP/2.0\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 505 ")


def test_expect_100_continue(cluster):
    with socket.create_connection(("127.0.0.1", cluster), timeout=5) as s:
        s.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 4\r\n\r\n")
        assert s.recv(1024) == b"HTTP/1.1 100 Continue\r\n\r\n"
        s.sendall(b"data")
        reply = b""
        while chunk := s.recv(4096):
            reply += chunk
    assert reply.startswith(b"HTTP/1.1 200 ")
    assert b'"body_bytes": 4' in reply


def test_dead_backend_gives_502():
    lb, port = start_lb([Backend("127.0.0.1", unused_port())])
    try:
        assert request(port)[0] == 502
    finally:
        lb.shutdown()


def test_no_backends_gives_503():
    lb, port = start_lb([])
    try:
        assert request(port)[0] == 503
    finally:
        lb.shutdown()


def test_slow_backend_gives_504(monkeypatch):
    monkeypatch.setattr(config, "BACKEND_READ_TIMEOUT", 0.2)
    slow = start_backend("slow", delay_ms=1000)
    lb, port = start_lb([as_backend(slow)])
    try:
        assert request(port)[0] == 504
    finally:
        lb.shutdown()
        slow.shutdown()
        slow.server_close()


def test_concurrent_requests_all_succeed(cluster):
    """Thread-per-connection: 30 simultaneous clients are all served."""
    results = []
    lock = threading.Lock()

    def client():
        status = request(cluster)[0]
        with lock:
            results.append(status)

    threads = [threading.Thread(target=client) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 30
