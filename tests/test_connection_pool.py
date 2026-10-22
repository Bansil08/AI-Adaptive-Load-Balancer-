"""Connection pool: reuse, size limit, connections the backend closed, and keep-alive framing."""

import json
import socket
import threading
import time

import pytest

from lb.config import PoolSettings, ProxySettings
from lb.connection_pool import ConnectionPool, peer_closed
from lb.http_parser import Headers, HTTPRequest
from lb.proxy import Proxy
from lb.router import Router
from lb.server import LoadBalancer
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_proxy import as_backend, request, start_backend

GET = HTTPRequest("GET", "/", "HTTP/1.1", Headers([("Host", "x")]), b"")


@pytest.fixture
def backend():
    server = start_backend("a")
    yield server
    server.shutdown()
    server.server_close()


def pooled_proxy(backends, size=4) -> Proxy:
    router = Router(backends, RoundRobinStrategy())
    return Proxy(router, ProxySettings(), ConnectionPool(PoolSettings(size=size)))


def test_sequential_requests_reuse_one_connection(backend):
    proxy = pooled_proxy([as_backend(backend)])
    for _ in range(10):
        assert proxy.route_and_forward(GET, "127.0.0.1").status == 200
    counters = proxy.pool.counters()
    assert (counters.created, counters.reused, counters.idle) == (1, 9, 1)


def test_without_pooling_every_request_opens_a_new_connection(backend):
    proxy = pooled_proxy([as_backend(backend)], size=0)
    for _ in range(5):
        assert proxy.route_and_forward(GET, "127.0.0.1").status == 200
    counters = proxy.pool.counters()
    assert (counters.created, counters.reused, counters.idle) == (5, 0, 0)


def test_idle_connections_are_capped_at_the_pool_size(backend):
    b = as_backend(backend)
    pool = ConnectionPool(PoolSettings(size=2))
    conns = [pool.acquire(b) for _ in range(4)]  # a burst: 4 at once, none blocked
    for conn in conns:
        pool.release(b, conn, reusable=True)
    counters = pool.counters()
    assert (counters.created, counters.idle) == (4, 2)  # the extra 2 were closed
    assert sum(c.sock.fileno() == -1 for c in conns) == 2


def test_unreusable_connections_are_closed_not_pooled(backend):
    b = as_backend(backend)
    pool = ConnectionPool(PoolSettings(size=2))
    conn = pool.acquire(b)
    pool.release(b, conn, reusable=False)
    assert pool.counters().idle == 0 and conn.sock.fileno() == -1


def test_peer_closed_sees_fin():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    server_side, _ = listener.accept()
    try:
        assert not peer_closed(client)       # open and idle
        server_side.close()                  # FIN
        time.sleep(0.05)
        assert peer_closed(client)
    finally:
        client.close()
        listener.close()


def test_idle_timeout_drops_old_connections(backend):
    b = as_backend(backend)
    pool = ConnectionPool(PoolSettings(size=2, idle_timeout=0.05))
    pool.release(b, pool.acquire(b), reusable=True)
    time.sleep(0.1)
    conn = pool.acquire(b)
    assert not conn.reused
    assert pool.counters().discarded == 1


def raw_backend(responses: list[bytes], received: list[bytes] | None = None):
    """A backend that answers the requests on each connection with the canned
    responses in order, then closes that connection (and accepts the next)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    accepted = []
    received = received if received is not None else []

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            accepted.append(conn)
            with conn:
                for reply in responses:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        data += chunk
                    received.append(data)
                    conn.sendall(reply)

    threading.Thread(target=serve, daemon=True).start()
    return sock, accepted


def test_chunked_response_is_framed_so_the_connection_is_reused():
    chunked = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\nX-Trailer: t\r\n\r\n")
    sock, accepted = raw_backend([chunked, chunked])
    try:
        proxy = pooled_proxy([Backend("127.0.0.1", sock.getsockname()[1])])
        for _ in range(2):
            result = proxy.route_and_forward(GET, "127.0.0.1")
            assert result.status == 200
            assert result.reply.endswith(b"5\r\nhello\r\n6;ext=1\r\n world\r\n0\r\nX-Trailer: t\r\n\r\n")
        assert len(accepted) == 1 and proxy.pool.counters().reused == 1
    finally:
        sock.close()


def test_connection_close_from_backend_is_not_reused():
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
    sock, accepted = raw_backend([reply])
    try:
        proxy = pooled_proxy([Backend("127.0.0.1", sock.getsockname()[1])])
        assert proxy.route_and_forward(GET, "127.0.0.1").status == 200
        assert proxy.pool.counters().idle == 0
    finally:
        sock.close()


def test_keep_alive_is_requested_from_the_backend():
    received = []
    sock, _ = raw_backend([b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"], received)
    try:
        pooled_proxy([Backend("127.0.0.1", sock.getsockname()[1])]).route_and_forward(GET, "127.0.0.1")
        assert b"\r\nConnection: keep-alive\r\n" in received[0]
    finally:
        sock.close()


def test_idle_connection_the_backend_closed_is_discarded_and_replaced():
    """The backend answers once per connection and then closes it, without
    saying so (as on a restart or its own idle timeout). The FIN sits in our
    idle socket; the peek before reuse sees it, so the next request goes out
    on a fresh connection instead of failing on the dead one."""
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    sock, accepted = raw_backend([reply])
    try:
        proxy = pooled_proxy([Backend("127.0.0.1", sock.getsockname()[1])])
        assert proxy.route_and_forward(GET, "127.0.0.1").status == 200
        time.sleep(0.05)  # let the FIN arrive
        assert proxy.route_and_forward(GET, "127.0.0.1").status == 200
        counters = proxy.pool.counters()
        assert (counters.created, counters.reused, counters.discarded) == (2, 0, 1)
    finally:
        sock.close()


def test_pooled_lb_end_to_end(backend):
    router = Router([as_backend(backend)], RoundRobinStrategy())
    pool = ConnectionPool(PoolSettings(size=4))
    lb = LoadBalancer(router, "127.0.0.1", 0, proxy=Proxy(router, pool=pool))
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        bodies = [request(port, "POST", "/", body=b"x" * 100)[2] for _ in range(5)]
        assert all(json.loads(b)["body_bytes"] == 100 for b in bodies)
        assert pool.counters().created == 1
    finally:
        lb.shutdown()


def closes_instead_of_answering_the_second_request():
    """Answers one request per connection, then closes when the next one arrives
    (as if its idle timeout fired at that moment): no FIN was waiting in the
    pool, so the peek can't catch it."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)

    def handle(conn):
        with conn:
            for answer in (True, False):
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                if answer:
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return sock


def test_get_on_a_connection_closed_at_reuse_is_resent_on_a_fresh_one():
    """Regression: this gave 502 although a new connection to the same backend works."""
    sock = closes_instead_of_answering_the_second_request()
    try:
        proxy = pooled_proxy([Backend("127.0.0.1", sock.getsockname()[1])])
        assert [proxy.route_and_forward(GET, "127.0.0.1").status for _ in range(3)] == [200] * 3
        counters = proxy.pool.counters()
        assert counters.created == 3  # the 1st, then one fresh connection per stale reuse
    finally:
        sock.close()


def test_post_on_a_connection_closed_at_reuse_is_not_resent():
    sock = closes_instead_of_answering_the_second_request()
    try:
        proxy = pooled_proxy([Backend("127.0.0.1", sock.getsockname()[1])])
        post = HTTPRequest("POST", "/", "HTTP/1.1", Headers([("Host", "x"), ("Content-Length", "0")]), b"")
        assert proxy.route_and_forward(post, "127.0.0.1").status == 200
        # The backend may have processed it before closing: never send a POST twice.
        assert proxy.route_and_forward(post, "127.0.0.1").status == 502
        assert proxy.pool.counters().created == 1
    finally:
        sock.close()


def test_nagle_is_off_on_backend_connections_and_in_the_test_backend(backend):
    """Regression: http.server writes headers and body separately; with Nagle on,
    the body waited for the (delayed) ACK of the headers on every pooled
    keep-alive response, ~46 ms per request against the Docker backends."""
    from backend.app import BackendHandler
    conn = ConnectionPool(PoolSettings(size=1)).connect(as_backend(backend))
    try:
        assert conn.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
    finally:
        conn.sock.close()
    assert BackendHandler.disable_nagle_algorithm is True
