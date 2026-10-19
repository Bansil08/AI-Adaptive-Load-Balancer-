"""Tests for the hand-written HTTP parser."""

import socket
import threading
import time

import pytest

from lb.http_parser import (
    Headers,
    ParseError,
    SocketReader,
    content_length,
    parse_request_head,
    parse_response_head,
    read_request,
    read_response,
    response_has_body,
)


# --- parse_request_head --------------------------------------------------------


def test_parses_request_line_and_headers():
    req = parse_request_head(b"GET /a?b=1 HTTP/1.1\r\nHost: example.com\r\nAccept: */*")
    assert (req.method, req.target, req.version) == ("GET", "/a?b=1", "HTTP/1.1")
    assert req.headers.get("host") == "example.com"  # case-insensitive lookup
    assert req.headers.get("ACCEPT") == "*/*"


def test_header_value_whitespace_is_trimmed_and_duplicates_kept():
    req = parse_request_head(b"GET / HTTP/1.1\r\nHost: x\r\nX-A:   1  \r\nX-A: 2")
    assert req.headers.get_all("x-a") == ["1", "2"]


def test_tab_and_obs_text_allowed_in_header_values():
    req = parse_request_head(b"GET /%C3%A9?q=1 HTTP/1.1\r\nHost: x\r\nX: a\tb\r\nY: caf\xe9")
    assert req.headers.get("x") == "a\tb"
    assert req.headers.get("y") == "caf\xe9"  # round-trips via latin-1
    assert req.target == "/%C3%A9?q=1"


def test_http_1_0_does_not_require_host():
    req = parse_request_head(b"GET / HTTP/1.0")
    assert req.version == "HTTP/1.0"


@pytest.mark.parametrize(
    "head, status",
    [
        (b"GET /\r\nHost: x", 400),                       # missing version
        (b"GET  / HTTP/1.1\r\nHost: x", 400),             # double space
        (b"get / HTTP/1.1\r\nHost: x", 400),              # lower-case method
        (b"GET http://x/ HTTP/1.1\r\nHost: x", 400),      # absolute-form target
        (b"GET / HTTP/2.0\r\nHost: x", 505),              # well-formed but unsupported
        (b"GET / HTTPS/1.1\r\nHost: x", 400),             # garbage version
        (b"GET / HTTP/1.1", 400),                         # HTTP/1.1 without Host
        (b"GET / HTTP/1.1\r\nHost: a\r\nHost: b", 400),   # two Host headers
        (b"GET / HTTP/1.1\r\nHost : x", 400),             # space before colon (smuggling)
        (b"GET / HTTP/1.1\r\nHost: x\r\n folded", 400),   # obsolete line folding
        (b"GET / HTTP/1.1\r\nHost: x\r\nNoColon", 400),
        (b"GET / HTTP/1.1\r\nHost: x\r\nX: a\nb", 400),   # bare LF inside a value
        (b"", 400),
        # Regressions: "$" in a regex also matches before a trailing "\n".
        (b"GET\n /x HTTP/1.1\r\nHost: x", 400),          # bare LF after method
        (b"GET / HTTP/1.1\r\nHost: x\r\nX\n: v", 400),   # bare LF in header name
        # Regressions: control / non-ASCII bytes in the target or header values.
        (b"GET /\x1b[31mFAKE HTTP/1.1\r\nHost: x", 400),  # ANSI escape (log injection)
        (b"GET /a\x00b HTTP/1.1\r\nHost: x", 400),        # NUL (Postgres rejects it)
        (b"GET /caf\xc3\xa9 HTTP/1.1\r\nHost: x", 400),   # raw UTF-8, must be %-encoded
        (b"GET / HTTP/1.1\r\nHost: x\r\nX: a\x1bb", 400),  # ESC in header value
        (b"GET / HTTP/1.1\r\nHost: x\r\nX: a\x7fb", 400),  # DEL in header value
    ],
)
def test_rejects_malformed_requests(head, status):
    with pytest.raises(ParseError) as exc:
        parse_request_head(head)
    assert exc.value.status_code == status


# --- Content-Length -------------------------------------------------------------


def test_content_length_values():
    assert content_length(Headers()) is None
    assert content_length(Headers([("Content-Length", "42")])) == 42
    # Repeated but identical is allowed.
    assert content_length(Headers([("Content-Length", "5"), ("content-length", "5")])) == 5


@pytest.mark.parametrize("values", [["5", "6"], ["-1"], ["abc"], ["²"], [" "], ["1e3"], ["5\n"]])
def test_content_length_rejects_bad_values(values):
    with pytest.raises(ParseError) as exc:
        content_length(Headers([("Content-Length", v) for v in values]))
    assert exc.value.status_code == 400


# --- responses ------------------------------------------------------------------


def test_parses_status_line_with_multiword_and_empty_reason():
    assert parse_response_head(b"HTTP/1.1 404 Not Found\r\nA: b").reason == "Not Found"
    resp = parse_response_head(b"HTTP/1.1 204")
    assert (resp.status_code, resp.reason) == (204, "")


@pytest.mark.parametrize("head", [b"HTTP/1.1 OK", b"HTTP/1.1 99 X", b"HTTP/1.1 600 X", b"FTP 200 OK"])
def test_rejects_malformed_status_line(head):
    with pytest.raises(ParseError) as exc:
        parse_response_head(head)
    assert exc.value.status_code == 502


@pytest.mark.parametrize(
    "method, status, expected",
    [("GET", 200, True), ("HEAD", 200, False), ("GET", 204, False), ("GET", 304, False), ("GET", 100, False)],
)
def test_response_has_body(method, status, expected):
    assert response_has_body(method, status) is expected


def test_serialisation_round_trip():
    raw = b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 3\r\n\r\nabc"
    head, _, body = raw.partition(b"\r\n\r\n")
    req = parse_request_head(head)
    req.body = body
    assert req.to_bytes() == raw


# --- reading from a real socket ----------------------------------------------------


@pytest.fixture
def sock_pair():
    """Two connected sockets: write to `peer`, read from `ours` through SocketReader."""
    ours, peer = socket.socketpair()
    ours.settimeout(2)
    yield ours, peer
    ours.close()
    peer.close()


def send_in_pieces(peer, data: bytes, piece: int, close: bool = True):
    """Send data a few bytes at a time to simulate TCP splitting a message."""

    def run():
        for i in range(0, len(data), piece):
            peer.sendall(data[i : i + piece])
            time.sleep(0.001)
        if close:
            peer.shutdown(socket.SHUT_WR)

    t = threading.Thread(target=run)
    t.start()
    return t


def test_request_split_across_many_recv_calls(sock_pair):
    ours, peer = sock_pair
    raw = b"POST /upload HTTP/1.1\r\nHost: h\r\nContent-Length: 11\r\n\r\nhello world"
    t = send_in_pieces(peer, raw, piece=3)
    req = read_request(SocketReader(ours))
    t.join()
    assert (req.method, req.target, req.body) == ("POST", "/upload", b"hello world")


def test_missing_content_length_means_empty_body(sock_pair):
    """No Content-Length (and no Transfer-Encoding) = no body, even for POST."""
    ours, peer = sock_pair
    peer.sendall(b"POST /a HTTP/1.1\r\nHost: h\r\n\r\nGET /b HTTP/1.1\r\nHost: h\r\n\r\n")
    peer.shutdown(socket.SHUT_WR)
    reader = SocketReader(ours)
    first = read_request(reader)
    assert (first.method, first.body) == ("POST", b"")
    # The next request's bytes were not swallowed as a body.
    assert read_request(reader).target == "/b"


def test_large_body_read_completely(sock_pair):
    """A 2 MB body arrives in many recv() calls and must be reassembled exactly."""
    ours, peer = sock_pair
    body = bytes(range(256)) * 8192  # 2 MiB, non-repeating within each 256 bytes
    raw = b"PUT /big HTTP/1.1\r\nHost: h\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    t = send_in_pieces(peer, raw, piece=65536)
    req = read_request(SocketReader(ours))
    t.join()
    assert len(req.body) == len(body) and req.body == body


def test_two_requests_in_one_send_are_both_read(sock_pair):
    """Bytes after the first message stay buffered for the next one."""
    ours, peer = sock_pair
    peer.sendall(
        b"POST /1 HTTP/1.1\r\nHost: h\r\nContent-Length: 2\r\n\r\nab"
        b"GET /2 HTTP/1.1\r\nHost: h\r\n\r\n"
    )
    peer.shutdown(socket.SHUT_WR)
    reader = SocketReader(ours)
    first, second = read_request(reader), read_request(reader)
    assert (first.target, first.body) == ("/1", b"ab")
    assert (second.target, second.body) == ("/2", b"")
    assert read_request(reader) is None  # clean close, no more requests


def test_clean_close_before_any_bytes_returns_none(sock_pair):
    ours, peer = sock_pair
    peer.shutdown(socket.SHUT_WR)
    assert read_request(SocketReader(ours)) is None


def test_close_in_middle_of_headers_is_an_error(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"GET / HTTP/1.1\r\nHost: h\r\n")
    peer.shutdown(socket.SHUT_WR)
    with pytest.raises(ParseError) as exc:
        read_request(SocketReader(ours))
    assert exc.value.status_code == 400


def test_truncated_body_is_an_error(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"POST / HTTP/1.1\r\nHost: h\r\nContent-Length: 10\r\n\r\nshort")
    peer.shutdown(socket.SHUT_WR)
    with pytest.raises(ParseError, match="5 of 10"):
        read_request(SocketReader(ours))


def test_oversized_headers_rejected_with_431(sock_pair):
    ours, peer = sock_pair
    t = send_in_pieces(peer, b"GET / HTTP/1.1\r\nX: " + b"a" * 5000, piece=1000, close=False)
    with pytest.raises(ParseError) as exc:
        read_request(SocketReader(ours, max_head=1024))
    t.join()
    assert exc.value.status_code == 431


def test_oversized_body_rejected_with_413_before_reading_it(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"POST / HTTP/1.1\r\nHost: h\r\nContent-Length: 999999\r\n\r\n")
    with pytest.raises(ParseError) as exc:
        read_request(SocketReader(ours), max_body=1000)
    assert exc.value.status_code == 413


def test_chunked_request_rejected_with_501(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"POST / HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\n\r\n")
    with pytest.raises(ParseError) as exc:
        read_request(SocketReader(ours))
    assert exc.value.status_code == 501


def test_expect_continue_callback_runs_before_body_is_read(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"POST / HTTP/1.1\r\nHost: h\r\nExpect: 100-continue\r\nContent-Length: 3\r\n\r\n")
    called = []

    def on_continue():
        called.append(True)
        peer.sendall(b"abc")  # a real client only sends the body after "100 Continue"

    req = read_request(SocketReader(ours), on_expect_continue=on_continue)
    assert called and req.body == b"abc"


def test_response_with_content_length(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
    resp = read_response(SocketReader(ours), "GET")
    assert (resp.status_code, resp.body) == (200, b"hi")


def test_response_to_head_has_no_body_despite_content_length(sock_pair):
    """Without the HEAD rule we would wait forever for 100 bytes that never come."""
    ours, peer = sock_pair
    peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n")
    resp = read_response(SocketReader(ours), "HEAD")
    assert resp.body == b""


def test_response_without_length_reads_until_close(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"HTTP/1.0 200 OK\r\n\r\nall of it")
    peer.shutdown(socket.SHUT_WR)
    assert read_response(SocketReader(ours), "GET").body == b"all of it"


def test_interim_100_continue_response_is_skipped(sock_pair):
    ours, peer = sock_pair
    peer.sendall(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 201 Created\r\nContent-Length: 0\r\n\r\n")
    assert read_response(SocketReader(ours), "POST").status_code == 201


def test_backend_closing_without_response_is_502(sock_pair):
    ours, peer = sock_pair
    peer.shutdown(socket.SHUT_WR)
    with pytest.raises(ParseError) as exc:
        read_response(SocketReader(ours), "GET")
    assert exc.value.status_code == 502
