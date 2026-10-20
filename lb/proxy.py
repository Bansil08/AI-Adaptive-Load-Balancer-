"""Forwarding logic: send each client request to a backend and relay the response.

Phase 1 opens a fresh TCP connection to the backend for every request and
closes the client connection after one response. Connection pooling, retries
and finer-grained timeouts come in Phase 3.
"""

import logging
import socket
import time
from http import HTTPStatus

from lb import config
from lb.config import ProxySettings
from lb.http_parser import (
    Headers,
    HTTPRequest,
    HTTPResponse,
    ParseError,
    SocketReader,
    read_request,
    read_response,
)
from lb.router import NoBackendAvailable, Router
from lb.strategies.base import Backend

log = logging.getLogger(__name__)

CONTINUE_RESPONSE = b"HTTP/1.1 100 Continue\r\n\r\n"

# Hop-by-hop headers describe one TCP connection (client<->LB or LB<->backend),
# not the end-to-end message, so a proxy must not forward them (RFC 9110 7.6.1).
# Transfer-Encoding is also per-hop in theory, but we pass response bodies
# through byte-for-byte, so the client needs it to decode them.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-connection", "te", "trailer",
    "upgrade", "proxy-authenticate", "proxy-authorization",
})
# A client could list these in "Connection: ..." to trick us into dropping them,
# which would change how the backend frames the message. Never strip them.
_NEVER_STRIP = frozenset({"host", "content-length", "transfer-encoding"})


def strip_hop_by_hop(headers: Headers) -> Headers:
    # "Connection: close, X-Foo" also marks X-Foo as hop-by-hop.
    named = {
        token.strip().lower()
        for value in headers.get_all("connection")
        for token in value.split(",")
    }
    drop = (HOP_BY_HOP | named) - _NEVER_STRIP
    return Headers((name, value) for name, value in headers if name.lower() not in drop)


def build_backend_request(request: HTTPRequest, client_ip: str) -> bytes:
    headers = strip_hop_by_hop(request.headers)
    # We already answered "100 Continue" ourselves and hold the whole body.
    headers.remove("expect")
    # Append rather than overwrite: if the client came through other proxies,
    # X-Forwarded-For already lists them and the backend should see the full chain.
    chain = headers.get_all("x-forwarded-for") + [client_ip]
    headers.set("X-Forwarded-For", ", ".join(chain))
    # New backend connection per request, so ask the backend to close it after
    # replying; that also lets us read bodies without a length up to EOF.
    headers.set("Connection", "close")
    return HTTPRequest(request.method, request.target, request.version, headers, request.body).to_bytes()


def forward(request_bytes: bytes, method: str, backend: Backend, settings: ProxySettings) -> HTTPResponse:
    """Send the request to the backend over a new TCP connection and read the response."""
    # create_connection resolves the name and performs the TCP 3-way handshake;
    # the timeout bounds how long we wait for the backend to accept.
    with socket.create_connection(
        (backend.host, backend.port), timeout=settings.backend_connect_timeout
    ) as sock:
        sock.settimeout(settings.backend_read_timeout)
        sock.sendall(request_bytes)
        return read_response(SocketReader(sock), method)


def client_response_bytes(response: HTTPResponse) -> bytes:
    headers = strip_hop_by_hop(response.headers)
    # One request per client connection in Phase 1 (keep-alive is out of scope).
    headers.set("Connection", "close")
    return HTTPResponse(response.version, response.status_code, response.reason, headers, response.body).to_bytes()


def error_response(status: int) -> bytes:
    """A small plain-text error page. Details go to the log, not to the client."""
    phrase = HTTPStatus(status).phrase
    body = f"{status} {phrase}\n".encode()
    headers = Headers([
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Connection", "close"),
    ])
    return HTTPResponse("HTTP/1.1", status, phrase, headers, body).to_bytes()


def _route_and_forward(
    request: HTTPRequest, client_ip: str, router: Router, settings: ProxySettings
) -> tuple[int, bytes, Backend | None]:
    """Returns (status code, bytes to send to the client, backend used)."""
    try:
        backend = router.choose(request)
    except NoBackendAvailable as exc:
        log.warning("503: %s", exc)
        return 503, error_response(503), None

    router.stats.request_started(backend)
    started = time.perf_counter()
    # Only a response or a timeout says how fast the backend is. A refused
    # connection fails in ~1 ms; counting it would make a dead backend look
    # like the fastest one to latency-based strategies.
    speed_known = True
    try:
        response = forward(build_backend_request(request, client_ip), request.method, backend, settings)
    # TimeoutError is a subclass of OSError, so it must be caught first.
    except TimeoutError:
        log.warning("504: backend %s timed out", backend)
        return 504, error_response(504), backend
    except ParseError as exc:
        speed_known = False
        log.warning("502: invalid response from backend %s: %s", backend, exc.message)
        return 502, error_response(502), backend
    except OSError as exc:  # connection refused/reset, host unreachable, ...
        speed_known = False
        log.warning("502: cannot reach backend %s: %s", backend, exc)
        return 502, error_response(502), backend
    finally:
        # In `finally` so a failed request still decrements active connections.
        elapsed_ms = (time.perf_counter() - started) * 1000
        router.stats.request_finished(backend, elapsed_ms if speed_known else None)
    return response.status_code, client_response_bytes(response), backend


def _send(sock: socket.socket, data: bytes) -> None:
    try:
        sock.sendall(data)
    except OSError as exc:
        # The client disconnected before we could answer; nothing more to do.
        log.debug("could not send to client: %s", exc)


def close_gracefully(sock: socket.socket) -> None:
    """Close without a TCP reset, so the client is sure to receive our response.

    If we close() while unread client bytes are still in our receive buffer
    (e.g. the body of a request we rejected with 413), the kernel sends RST
    instead of FIN, and the RST can destroy the response before the client
    reads it. So we send FIN with shutdown(SHUT_WR), read and discard whatever
    the client still sends until it closes too (or a deadline passes), and
    only then close(). This is the "lingering close" that Apache/nginx use.
    """
    try:
        sock.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + config.LINGER_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            if not sock.recv(64 * 1024):
                break  # client closed its side: clean four-way close
    except OSError:
        pass  # client already gone or deadline hit; nothing more to do
    finally:
        sock.close()


def handle_client(
    client_sock: socket.socket,
    client_addr: tuple,
    router: Router,
    settings: ProxySettings = ProxySettings(),
) -> None:
    """Serve one client connection: read one request, proxy it, reply, close."""
    client_ip = client_addr[0]
    start = time.perf_counter()
    try:
        client_sock.settimeout(settings.client_timeout)
        try:
            request = read_request(
                SocketReader(client_sock, deadline=time.monotonic() + settings.client_timeout),
                on_expect_continue=lambda: client_sock.sendall(CONTINUE_RESPONSE),
            )
        except ParseError as exc:
            log.info("%s rejected: %d %s", client_ip, exc.status_code, exc.message)
            _send(client_sock, error_response(exc.status_code))
            return
        except TimeoutError:
            log.info("%s timed out sending its request", client_ip)
            _send(client_sock, error_response(408))
            return
        except OSError as exc:  # e.g. connection reset by the client
            log.debug("%s connection error while reading: %s", client_ip, exc)
            return
        if request is None:
            return  # connected and closed without sending anything

        status, reply, backend = _route_and_forward(request, client_ip, router, settings)
        _send(client_sock, reply)
        # Measured before the lingering close: that wait is not request latency.
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.info(
            "%s %s %s -> %s %d %.1fms",
            client_ip, request.method, request.target, backend or "-", status, elapsed_ms,
        )
    finally:
        close_gracefully(client_sock)
