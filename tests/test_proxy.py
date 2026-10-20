"""End-to-end tests: real backends + real load balancer over localhost TCP."""

import errno
from collections import Counter
import http.client
import json
import socket
import threading

import pytest

from backend.app import BackendServer
from lb.config import ConcurrencySettings, ProxySettings
from lb.router import Router
from lb import server as server_module
from lb.server import LoadBalancer
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy


def start_backend(name: str, delay_ms: float = 0) -> BackendServer:
    server = BackendServer(("127.0.0.1", 0), name, delay_ms=delay_ms, quiet=True)
    # Short poll interval so shutdown() in teardown returns quickly.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


def start_lb(
    backends: list[Backend], settings: ProxySettings = ProxySettings(), **kwargs
) -> tuple[LoadBalancer, int]:
    lb = LoadBalancer(Router(backends, RoundRobinStrategy()), "127.0.0.1", 0, settings, **kwargs)
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


def test_requests_are_spread_evenly(cluster):
    counts = Counter(json.loads(request(cluster)[2])["server"] for _ in range(30))
    assert counts == {"a": 10, "b": 10, "c": 10}


def test_get_root_reports_name_and_delay():
    backend = start_backend("named", delay_ms=25)
    lb, port = start_lb([as_backend(backend)])
    try:
        payload = json.loads(request(port)[2])
        assert payload["server"] == "named" and payload["delay_ms"] == 25
    finally:
        lb.shutdown()
        backend.shutdown()
        backend.server_close()


def test_rejected_upload_still_receives_its_response(cluster):
    """Regression: the LB answered 413 then close()d with the unread body still
    in its receive buffer, so the kernel sent RST and a client that uploads
    first and reads afterwards (like http.client) got BrokenPipeError instead
    of the 413. The lingering close fixes that."""
    conn = http.client.HTTPConnection("127.0.0.1", cluster, timeout=10)
    try:
        conn.request("POST", "/", body=b"x" * (11 * 1024 * 1024))  # over the 10 MB limit
        assert conn.getresponse().status == 413
    finally:
        conn.close()


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
        # Read until the blank line rather than trusting one recv(): TCP may split it.
        interim = b""
        while not interim.endswith(b"\r\n\r\n"):
            interim += s.recv(1)
        assert interim == b"HTTP/1.1 100 Continue\r\n\r\n"
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


def test_stats_track_requests_and_skip_latency_of_refused_connections():
    good = start_backend("good", delay_ms=30)
    dead = Backend("127.0.0.1", unused_port())
    lb, port = start_lb([as_backend(good), dead])
    try:
        assert [request(port)[0] for _ in range(4)] == [200, 502, 200, 502]
        stats = lb.router.stats.snapshot()
        good_stats = stats[as_backend(good)]
        assert (good_stats.active_connections, good_stats.total_requests) == (0, 2)
        assert good_stats.avg_latency_ms >= 30
        # Refused in ~1 ms: counted as requests, but not as "a fast backend".
        assert (stats[dead].active_connections, stats[dead].total_requests) == (0, 2)
        assert stats[dead].avg_latency_ms is None
    finally:
        lb.shutdown()
        good.shutdown()
        good.server_close()


def test_no_backends_gives_503():
    lb, port = start_lb([])
    try:
        assert request(port)[0] == 503
    finally:
        lb.shutdown()


def test_slow_backend_gives_504():
    slow = start_backend("slow", delay_ms=1000)
    lb, port = start_lb([as_backend(slow)], ProxySettings(backend_read_timeout=0.2))
    try:
        assert request(port)[0] == 504
    finally:
        lb.shutdown()
        slow.shutdown()
        slow.server_close()


@pytest.mark.parametrize("thread_per_connection", [False, True], ids=["pool", "thread-per-connection"])
def test_concurrent_requests_all_succeed(thread_per_connection):
    """30 simultaneous clients are all served, in both modes. The pool has only
    4 workers and a 2-slot queue, so most clients wait in the listen backlog."""
    backend = start_backend("a", delay_ms=20)
    lb, port = start_lb(
        [as_backend(backend)],
        concurrency=ConcurrencySettings(workers=4, queue_size=2),
        thread_per_connection=thread_per_connection,
    )
    results = []
    lock = threading.Lock()

    def client():
        status = request(port)[0]
        with lock:
            results.append(status)

    threads = [threading.Thread(target=client) for _ in range(30)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    finally:
        lb.shutdown()
        backend.shutdown()
        backend.server_close()
    assert results == [200] * 30


class FailFirstAccept:
    """Wraps the listening socket so the first accept() fails like EMFILE would."""

    def __init__(self, sock):
        self._sock = sock
        self.failed = False

    def accept(self):
        if not self.failed:
            self.failed = True
            raise OSError(errno.EMFILE, "Too many open files")
        return self._sock.accept()

    def close(self):
        self._sock.close()


def test_accept_error_does_not_crash_the_lb():
    """Regression: an uncaught EMFILE from accept() used to kill the whole process."""
    backend = start_backend("a")
    lb = LoadBalancer(Router([as_backend(backend)], RoundRobinStrategy()), "127.0.0.1", 0)
    _, port = lb.start()
    flaky = lb._sock = FailFirstAccept(lb._sock)
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        # The fake failure doesn't consume our connection, so it is accepted on the retry.
        assert request(port)[0] == 200
        assert flaky.failed
    finally:
        lb.shutdown()
        backend.shutdown()
        backend.server_close()


def test_thread_start_failure_drops_one_connection_not_the_lb(monkeypatch):
    """Regression: "can't start new thread" must cost one connection, not the server.
    Only thread-per-connection starts threads per request; the pool's are made up front."""
    backend = start_backend("a")
    lb, port = start_lb([as_backend(backend)], thread_per_connection=True)

    class FailOnceThread(threading.Thread):
        failures_left = 1

        def start(self):
            if FailOnceThread.failures_left:
                FailOnceThread.failures_left -= 1
                raise RuntimeError("can't start new thread")
            super().start()

    try:
        # This patches threading.Thread globally, but the LB's handler thread is
        # the first one started, so it takes the single failure.
        monkeypatch.setattr(server_module.threading, "Thread", FailOnceThread)
        with pytest.raises(ConnectionError):  # this client's connection is closed on it
            request(port)
        monkeypatch.undo()
        assert request(port)[0] == 200  # the LB is still serving
    finally:
        lb.shutdown()
        backend.shutdown()
        backend.server_close()
