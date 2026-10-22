"""Forwarding logic: send each client request to a backend and relay the response.

Per client connection: read one request, check the client's rate limit, pick
a backend (router), forward over a pooled backend connection, retry
idempotent requests on another backend if there was no usable answer, reply,
and close the client connection (client-side keep-alive is out of scope;
pooling applies to LB -> backend connections).

Retries: only GET and HEAD, because they are idempotent: doing them twice
has the same effect as doing them once. A POST may have been processed by the
backend before the connection broke; retrying it could e.g. place an order
twice, so it is never retried. A retry goes to a backend not tried yet for
this request, after an exponential backoff (base, 2*base, 4*base, ... capped),
so a struggling cluster isn't hit by an instant retry storm. Only failures
WITHOUT a usable response are retried (refused, reset, timeout, garbage): an
HTTP response, even a 500, is the backend's answer and is passed through.
"""

import logging
import socket
import time
from dataclasses import dataclass
from http import HTTPStatus

from lb import config
from lb.config import PoolSettings, ProxySettings, RetrySettings
from lb.connection_pool import ConnectionPool
from lb.rate_limiter import RateLimiter
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

# Safe to send twice (RFC 9110 9.2.2). PUT and DELETE are idempotent too, but
# the spec for this project limits retries to the read-only ones.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def is_retryable(method: str) -> bool:
    return method in IDEMPOTENT_METHODS


def backoff_delay(retry_number: int, settings: RetrySettings) -> float:
    """Wait before retry number 1, 2, 3...: base, 2*base, 4*base, ... capped at backoff_max."""
    return min(settings.backoff_max, settings.backoff_base * 2 ** (retry_number - 1))


@dataclass(frozen=True)
class ProxyResult:
    status: int
    reply: bytes               # bytes to send to the client
    backend: Backend | None    # the backend that produced the final answer (None: none tried)
    attempts: int              # backends tried; 0 if none was available

    @property
    def retried(self) -> bool:
        return self.attempts > 1

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


def build_backend_request(request: HTTPRequest, client_ip: str, keep_alive: bool = False) -> bytes:
    headers = strip_hop_by_hop(request.headers)
    # We already answered "100 Continue" ourselves and hold the whole body.
    headers.remove("expect")
    # Append rather than overwrite: if the client came through other proxies,
    # X-Forwarded-For already lists them and the backend should see the full chain.
    chain = headers.get_all("x-forwarded-for") + [client_ip]
    headers.set("X-Forwarded-For", ", ".join(chain))
    # With pooling, ask the backend to keep the connection open for the next
    # request; without it, to close it after replying (one connection per request).
    headers.set("Connection", "keep-alive" if keep_alive else "close")
    return HTTPRequest(request.method, request.target, request.version, headers, request.body).to_bytes()


def can_reuse(response: HTTPResponse, reader: SocketReader) -> bool:
    """May this backend connection carry another request?"""
    tokens = {t.strip().lower() for v in response.headers.get_all("connection") for t in v.split(",")}
    return (
        response.version == "HTTP/1.1"   # HTTP/1.0 closes unless both sides negotiate otherwise
        and "close" not in tokens        # the backend said it will close
        and not response.framed_by_close # the body ended BY the close
        and not reader.has_buffered()    # stray bytes: we no longer know where the next response starts
    )


def forward(request_bytes: bytes, method: str, backend: Backend, settings: ProxySettings,
            pool: ConnectionPool) -> HTTPResponse:
    """Send the request over a pooled (or new) connection and read the response."""
    # A new connection costs the TCP 3-way handshake; connect timeout bounds it.
    conn = pool.acquire(backend)
    try:
        conn.sock.settimeout(settings.backend_read_timeout)
        conn.sock.sendall(request_bytes)
        reader = SocketReader(conn.sock)
        response = read_response(reader, method)
    except BaseException:
        # Half-read or broken: the connection's state is unknown, never pool it.
        conn.sock.close()
        raise
    pool.release(backend, conn, reusable=can_reuse(response, reader))
    return response


def client_response_bytes(response: HTTPResponse) -> bytes:
    headers = strip_hop_by_hop(response.headers)
    # One request per client connection in Phase 1 (keep-alive is out of scope).
    headers.set("Connection", "close")
    return HTTPResponse(response.version, response.status_code, response.reason, headers, response.body).to_bytes()


def error_response(status: int, extra_headers: tuple[tuple[str, str], ...] = ()) -> bytes:
    """A small plain-text error page. Details go to the log, not to the client."""
    phrase = HTTPStatus(status).phrase
    body = f"{status} {phrase}\n".encode()
    headers = Headers([
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(body))),
        *extra_headers,
        ("Connection", "close"),
    ])
    return HTTPResponse("HTTP/1.1", status, phrase, headers, body).to_bytes()


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


class Proxy:
    """Everything one client connection needs: router, timeouts, pool, retries, rate limiter.

    The defaults reproduce the Phase 1/2 behaviour (a new backend connection per
    request, no retries, no rate limit); lb/server.py main() turns the Phase 3
    features on from lb.toml.
    """

    def __init__(
        self,
        router: Router,
        settings: ProxySettings = ProxySettings(),
        pool: ConnectionPool | None = None,
        retry: RetrySettings = RetrySettings(),
        rate_limiter: RateLimiter | None = None,
    ):
        self.router = router
        self.settings = settings
        self.pool = pool or ConnectionPool(PoolSettings(size=0), settings.backend_connect_timeout)
        self.retry = retry
        self.rate_limiter = rate_limiter

    def route_and_forward(self, request: HTTPRequest, client_ip: str) -> ProxyResult:
        request_bytes = build_backend_request(request, client_ip, keep_alive=self.pool.enabled)
        max_attempts = 1 + (self.retry.max_retries if is_retryable(request.method) else 0)
        tried: list[Backend] = []
        status = 503
        for attempt in range(max_attempts):
            if attempt:
                time.sleep(backoff_delay(attempt, self.retry))
            try:
                backend = self.router.acquire(request, client_ip, exclude=tried)
            except NoBackendAvailable as exc:
                if not tried:
                    log.warning("503: %s", exc)
                break  # nothing (left) to try: answer with the last failure
            tried.append(backend)
            outcome = self._attempt(request_bytes, request.method, backend)
            if isinstance(outcome, HTTPResponse):
                return ProxyResult(outcome.status_code, client_response_bytes(outcome), backend, len(tried))
            status = outcome
            if attempt + 1 < max_attempts:
                log.info("retrying %s %s after %d from %s", request.method, request.target, status, backend)
        return ProxyResult(status, error_response(status), tried[-1] if tried else None, len(tried))

    def _attempt(self, request_bytes: bytes, method: str, backend: Backend) -> HTTPResponse | int:
        """One try on one leased backend: the response, or the error status to report."""
        self.router.stats.request_started(backend)
        started = time.perf_counter()
        # Only a response or a timeout says how fast the backend is. A refused
        # connection fails in ~1 ms; counting it would make a dead backend look
        # like the fastest one to latency-based strategies.
        speed_known = True
        ok = False  # outcome reported to the circuit breaker
        try:
            response = forward(request_bytes, method, backend, self.settings, self.pool)
            ok = response.status_code < 500
            return response
        # TimeoutError is a subclass of OSError, so it must be caught first.
        except TimeoutError:
            log.warning("504: backend %s timed out", backend)
            return 504
        except ParseError as exc:
            speed_known = False
            log.warning("502: invalid response from backend %s: %s", backend, exc.message)
            return 502
        except OSError as exc:  # connection refused/reset, host unreachable, ...
            speed_known = False
            log.warning("502: cannot reach backend %s: %s", backend, exc)
            return 502
        finally:
            # In `finally` so a failed request still decrements active
            # connections and ends its lease (or a drain would wait forever).
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.router.stats.request_finished(backend, elapsed_ms if speed_known else None)
            self.router.release(backend, ok)

    def handle_client(self, client_sock: socket.socket, client_addr: tuple) -> None:
        """Serve one client connection: read one request, proxy it, reply, close."""
        client_ip = client_addr[0]
        settings = self.settings
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

            if self.rate_limiter and not self.rate_limiter.allow(client_ip):
                # Checked after reading the request so the rejection can be logged
                # with its method and path (Phase 4 stores it).
                retry_after = str(self.rate_limiter.retry_after(client_ip))
                _send(client_sock, error_response(429, (("Retry-After", retry_after),)))
                log.info("%s %s %s -> - 429 rate limited", client_ip, request.method, request.target)
                return

            result = self.route_and_forward(request, client_ip)
            _send(client_sock, result.reply)
            # Measured before the lingering close: that wait is not request latency.
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.info(
                "%s %s %s -> %s %d %.1fms%s",
                client_ip, request.method, request.target, result.backend or "-", result.status,
                elapsed_ms, f" (retried, {result.attempts} attempts)" if result.retried else "",
            )
        finally:
            close_gracefully(client_sock)
