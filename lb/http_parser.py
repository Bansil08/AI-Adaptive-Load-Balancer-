"""Hand-written HTTP/1.1 request and response parsing (request line, headers, body).

TCP delivers a byte *stream* with no message boundaries: one recv() may return
half a header, or the end of one request plus the start of the next. So
SocketReader keeps a buffer and reads "until the blank line that ends the
headers", then "exactly Content-Length more bytes".

The parse_* functions work on bytes only, so they can be unit-tested without
sockets. They are deliberately strict where lenient parsing is a known
security risk (HTTP request smuggling): we reject rather than guess.
"""

import re
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from lb import config

CRLF = b"\r\n"
HEAD_END = b"\r\n\r\n"
RECV_SIZE = 64 * 1024

# All patterns below are used with fullmatch(), never match() with "^...$":
# "$" also matches just before a trailing "\n", so r"^[0-9]+$" would accept "5\n".

# RFC 9110 "token": the characters allowed in a header name.
_TOKEN_RE = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# Methods are case-sensitive tokens. We accept upper-case ones (matches the DB CHECK).
_METHOD_RE = re.compile(r"[A-Z]{1,16}")
# Not str.isdigit(): that also accepts characters like '²'.
_DIGITS_RE = re.compile(r"[0-9]+")
_VERSION_RE = re.compile(r"HTTP/[0-9]\.[0-9]")
_STATUS_RE = re.compile(r"[1-5][0-9]{2}")
# Request target: "/" then visible ASCII only. Anything else (spaces, raw UTF-8,
# control bytes) must be percent-encoded by the client. This also keeps escape
# codes out of our logs and NUL bytes (which Postgres rejects) out of the DB.
_TARGET_RE = re.compile(r"/[\x21-\x7e]*")
# Control characters are illegal in header values; only TAB is allowed (RFC 9110 5.5).
_VALUE_CTL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
SUPPORTED_VERSIONS = ("HTTP/1.0", "HTTP/1.1")


class ParseError(Exception):
    """Malformed or unsupported message; status_code is what we answer the client with."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class Headers:
    """Ordered header list with case-insensitive lookup.

    A list of (name, value) pairs rather than a dict, because order is preserved
    and some headers legitimately repeat (e.g. two Set-Cookie lines).
    """

    def __init__(self, items: Iterable[tuple[str, str]] = ()):
        self._items = list(items)

    def get(self, name: str, default: str | None = None) -> str | None:
        for key, value in self._items:
            if key.lower() == name.lower():
                return value
        return default

    def get_all(self, name: str) -> list[str]:
        return [value for key, value in self._items if key.lower() == name.lower()]

    def add(self, name: str, value: str) -> None:
        self._items.append((name, value))

    def remove(self, name: str) -> None:
        self._items = [(k, v) for k, v in self._items if k.lower() != name.lower()]

    def set(self, name: str, value: str) -> None:
        self.remove(name)
        self.add(name, value)

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def to_bytes(self) -> bytes:
        return b"".join(f"{k}: {v}\r\n".encode("latin-1") for k, v in self._items)


@dataclass
class HTTPRequest:
    method: str
    target: str
    version: str
    headers: Headers
    body: bytes = b""

    def to_bytes(self) -> bytes:
        line = f"{self.method} {self.target} {self.version}\r\n".encode("latin-1")
        return line + self.headers.to_bytes() + CRLF + self.body


@dataclass
class HTTPResponse:
    version: str
    status_code: int
    reason: str
    headers: Headers = field(default_factory=Headers)
    body: bytes = b""

    def to_bytes(self) -> bytes:
        line = f"{self.version} {self.status_code} {self.reason}\r\n".encode("latin-1")
        return line + self.headers.to_bytes() + CRLF + self.body


# --- Pure parsing (bytes -> objects) -----------------------------------------


def _decode(raw: bytes) -> str:
    # Latin-1 maps every byte to exactly one character, so decoding never fails
    # and encoding back gives the original bytes unchanged.
    return raw.decode("latin-1")


def _parse_header_lines(lines: list[bytes], error_status: int) -> Headers:
    headers = Headers()
    for raw in lines:
        if raw[:1] in (b" ", b"\t"):
            # Obsolete "line folding" (a header continued on the next line) is
            # forbidden by RFC 9112 because parsers disagree on how to join it.
            raise ParseError(error_status, "obsolete header line folding")
        name, sep, value = _decode(raw).partition(":")
        if not sep:
            raise ParseError(error_status, "header line without a colon")
        if not _TOKEN_RE.fullmatch(name):
            # Also rejects "Host : x": whitespace before the colon is a smuggling trick.
            raise ParseError(error_status, f"invalid header name {name!r}")
        value = value.strip(" \t")
        if _VALUE_CTL_RE.search(value):
            raise ParseError(error_status, f"control character in header {name!r}")
        headers.add(name, value)
    return headers


def parse_request_head(head: bytes) -> HTTPRequest:
    """Parse the request line and headers (everything before the blank line)."""
    lines = head.split(CRLF)
    parts = _decode(lines[0]).split(" ")
    if len(parts) != 3:
        raise ParseError(400, "malformed request line")
    method, target, version = parts

    if not _METHOD_RE.fullmatch(method):
        raise ParseError(400, f"invalid method {method!r}")
    if not _TARGET_RE.fullmatch(target):
        # A reverse proxy only needs "origin-form" targets like /path?query.
        raise ParseError(400, "request target must be '/' followed by visible ASCII")
    if version not in SUPPORTED_VERSIONS:
        if _VERSION_RE.fullmatch(version):
            raise ParseError(505, f"unsupported HTTP version {version}")
        raise ParseError(400, f"malformed HTTP version {version!r}")

    headers = _parse_header_lines(lines[1:], error_status=400)
    if version == "HTTP/1.1" and len(headers.get_all("host")) != 1:
        raise ParseError(400, "HTTP/1.1 requires exactly one Host header")
    return HTTPRequest(method, target, version, headers)


def parse_response_head(head: bytes) -> HTTPResponse:
    """Parse the status line and headers of a backend response."""
    lines = head.split(CRLF)
    # The reason phrase may contain spaces or be empty: "HTTP/1.1 404 Not Found".
    parts = _decode(lines[0]).split(" ", 2)
    if len(parts) < 2 or parts[0] not in SUPPORTED_VERSIONS or not _STATUS_RE.fullmatch(parts[1]):
        raise ParseError(502, "malformed status line from backend")
    reason = parts[2] if len(parts) == 3 else ""
    headers = _parse_header_lines(lines[1:], error_status=502)
    return HTTPResponse(parts[0], int(parts[1]), reason, headers)


def content_length(headers: Headers, error_status: int = 400) -> int | None:
    """Return the Content-Length value, or None if absent."""
    values = headers.get_all("content-length")
    if not values:
        return None
    if len(set(values)) != 1:
        # Two different lengths = two different ideas of where the body ends.
        raise ParseError(error_status, "conflicting Content-Length headers")
    if not _DIGITS_RE.fullmatch(values[0]):
        raise ParseError(error_status, f"invalid Content-Length {values[0]!r}")
    return int(values[0])


def response_has_body(request_method: str, status_code: int) -> bool:
    """RFC 9110: these responses never have a body, whatever their headers say."""
    return not (
        request_method == "HEAD"
        or 100 <= status_code < 200
        or status_code in (204, 304)
    )


# --- Socket reading -----------------------------------------------------------


class SocketReader:
    """Buffered reader over a TCP socket.

    Bytes received beyond the current message stay in the buffer, so nothing
    is lost if the peer sends the next message in the same TCP segment.
    """

    def __init__(self, sock: socket.socket, max_head: int = config.MAX_HEADER_BYTES):
        self._sock = sock
        self._max_head = max_head
        self._buf = bytearray()

    def _recv(self) -> bytes:
        return self._sock.recv(RECV_SIZE)

    def read_head(self) -> bytes | None:
        """Read up to the blank line ending the headers. None if the peer closed cleanly first."""
        while True:
            end = self._buf.find(HEAD_END)
            if end != -1:
                if end > self._max_head:
                    raise ParseError(431, "request header section too large")
                head = bytes(self._buf[:end])
                del self._buf[: end + len(HEAD_END)]
                return head
            if len(self._buf) > self._max_head:
                raise ParseError(431, "request header section too large")
            chunk = self._recv()
            if not chunk:  # recv() returning b"" means the peer closed its side
                if not self._buf:
                    return None
                raise ParseError(400, "connection closed in the middle of the headers")
            self._buf += chunk

    def read_exact(self, n: int) -> bytes:
        """Read exactly n body bytes (some may already be buffered)."""
        while len(self._buf) < n:
            chunk = self._recv()
            if not chunk:
                raise ParseError(400, f"body ended after {len(self._buf)} of {n} bytes")
            self._buf += chunk
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def read_until_close(self, limit: int) -> bytes:
        """Read until the peer closes the connection (body with no declared length)."""
        while True:
            chunk = self._recv()
            if not chunk:
                break
            self._buf += chunk
            if len(self._buf) > limit:
                raise ParseError(502, "response body too large")
        data = bytes(self._buf)
        self._buf.clear()
        return data


def read_request(
    reader: SocketReader,
    on_expect_continue: Callable[[], None] | None = None,
    max_body: int = config.MAX_BODY_BYTES,
) -> HTTPRequest | None:
    """Read one full request (head + body). None if the client closed without sending one."""
    head = reader.read_head()
    if head is None:
        return None
    request = parse_request_head(head)

    if "transfer-encoding" in request.headers:
        # Chunked request bodies are out of scope. Also, Transfer-Encoding plus
        # Content-Length is the classic smuggling ambiguity, so refuse outright.
        raise ParseError(501, "Transfer-Encoding in requests is not supported")

    length = content_length(request.headers) or 0
    if length > max_body:
        raise ParseError(413, f"body of {length} bytes exceeds limit of {max_body}")
    if length and on_expect_continue and (request.headers.get("expect") or "").lower() == "100-continue":
        # The client is waiting for permission before sending a large body.
        on_expect_continue()
    request.body = reader.read_exact(length)
    return request


def read_response(
    reader: SocketReader,
    request_method: str,
    max_body: int = config.MAX_BODY_BYTES,
) -> HTTPResponse:
    """Read one full response from a backend that was asked to close afterwards."""
    while True:
        head = reader.read_head()
        if head is None:
            raise ParseError(502, "backend closed the connection without responding")
        response = parse_response_head(head)
        # 1xx responses are interim ("100 Continue"); the real answer follows.
        if response.status_code >= 200:
            break

    if not response_has_body(request_method, response.status_code):
        return response

    if "transfer-encoding" in response.headers:
        # e.g. chunked: the body delimits itself. We asked the backend to close
        # after responding, so reading to EOF captures it exactly; it is passed
        # through unchanged and the client decodes the chunks.
        response.body = reader.read_until_close(max_body)
        return response

    length = content_length(response.headers, error_status=502)
    if length is None:
        # No length declared: HTTP/1.x then says the body ends when the connection closes.
        response.body = reader.read_until_close(max_body)
    elif length > max_body:
        raise ParseError(502, "response body too large")
    else:
        try:
            response.body = reader.read_exact(length)
        except ParseError as exc:
            raise ParseError(502, exc.message) from None
    return response
