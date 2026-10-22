"""Retry rules: GET/HEAD retried on another backend with backoff, POST never, retried flag recorded."""

import socket
import threading
import time

import pytest

from lb.config import RetrySettings
from lb.http_parser import Headers, HTTPRequest
from lb.proxy import Proxy, backoff_delay, is_retryable
from lb.router import Router
from lb.server import LoadBalancer
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_proxy import as_backend, request, start_backend, unused_port

RETRY = RetrySettings(max_retries=2, backoff_base=0.01, backoff_max=0.04)


def make_request(method: str) -> HTTPRequest:
    return HTTPRequest(method, "/", "HTTP/1.1", Headers([("Host", "x")]), b"")


@pytest.fixture
def good():
    server = start_backend("good")
    yield server
    server.shutdown()
    server.server_close()


def proxy_for(backends, retry=RETRY) -> Proxy:
    # Round robin starts at the first backend, so a dead one listed first is tried first.
    return Proxy(Router(backends, RoundRobinStrategy()), retry=retry)


def test_only_get_and_head_are_retryable():
    assert is_retryable("GET") and is_retryable("HEAD")
    assert not any(is_retryable(m) for m in ("POST", "PUT", "PATCH", "DELETE"))


def test_backoff_doubles_and_is_capped():
    settings = RetrySettings(backoff_base=0.1, backoff_max=0.5)
    assert [backoff_delay(n, settings) for n in (1, 2, 3, 4, 5)] == [0.1, 0.2, 0.4, 0.5, 0.5]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_idempotent_request_is_retried_on_another_backend(good, method):
    dead = Backend("127.0.0.1", unused_port())
    proxy = proxy_for([dead, as_backend(good)])
    result = proxy.route_and_forward(make_request(method), "127.0.0.1")
    assert result.status == 200
    assert result.backend == as_backend(good)
    assert result.retried and result.attempts == 2


def test_post_is_never_retried(good):
    dead = Backend("127.0.0.1", unused_port())
    proxy = proxy_for([dead, as_backend(good)])
    result = proxy.route_and_forward(make_request("POST"), "127.0.0.1")
    assert result.status == 502
    assert result.backend == dead
    assert not result.retried and result.attempts == 1


def test_first_try_success_is_not_marked_retried(good):
    result = proxy_for([as_backend(good)]).route_and_forward(make_request("GET"), "127.0.0.1")
    assert result.status == 200 and not result.retried


def test_retries_stop_at_max_retries_and_each_backend_is_tried_once():
    dead = [Backend("127.0.0.1", unused_port()) for _ in range(5)]
    proxy = proxy_for(dead)
    result = proxy.route_and_forward(make_request("GET"), "127.0.0.1")
    assert result.status == 502
    assert result.attempts == 1 + RETRY.max_retries
    totals = proxy.router.stats.snapshot()
    assert sorted(s.total_requests for s in totals.values()) == [0, 0, 1, 1, 1]


def test_no_other_backend_left_returns_the_last_error():
    proxy = proxy_for([Backend("127.0.0.1", unused_port())])
    result = proxy.route_and_forward(make_request("GET"), "127.0.0.1")
    assert (result.status, result.attempts) == (502, 1)


def test_backoff_is_applied_between_attempts():
    dead = [Backend("127.0.0.1", unused_port()) for _ in range(3)]
    proxy = proxy_for(dead, RetrySettings(max_retries=2, backoff_base=0.1, backoff_max=1.0))
    started = time.monotonic()
    proxy.route_and_forward(make_request("GET"), "127.0.0.1")
    assert time.monotonic() - started >= 0.1 + 0.2


def test_an_http_error_response_is_passed_through_not_retried(good):
    """The backend answered (even if with 500), so retrying could repeat work it did."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)

    def answer_500():
        conn, _ = sock.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")

    threading.Thread(target=answer_500, daemon=True).start()
    try:
        proxy = proxy_for([Backend("127.0.0.1", sock.getsockname()[1]), as_backend(good)])
        result = proxy.route_and_forward(make_request("GET"), "127.0.0.1")
        assert (result.status, result.attempts) == (500, 1)
    finally:
        sock.close()


def test_end_to_end_gets_survive_a_dead_backend_but_posts_do_not(good):
    dead = Backend("127.0.0.1", unused_port())
    router = Router([dead, as_backend(good)], RoundRobinStrategy())
    lb = LoadBalancer(router, "127.0.0.1", 0, proxy=Proxy(router, retry=RETRY))
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        assert [request(port)[0] for _ in range(6)] == [200] * 6
        posts = [request(port, "POST", "/", body=b"x")[0] for _ in range(4)]
        # Every POST that round robin sends to the dead backend fails: never retried.
        assert 502 in posts and set(posts) <= {200, 502}
    finally:
        lb.shutdown()
