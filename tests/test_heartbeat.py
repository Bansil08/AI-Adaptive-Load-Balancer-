"""UDP heartbeats: validation, ordering, and a real agent -> listener round trip."""

import json
import socket
import threading
import time

import pytest

from backend.agent import HeartbeatAgent
from lb.heartbeat import HeartbeatListener, parse_heartbeat
from lb.router import Router
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_proxy import request, start_backend

B = Backend("127.0.0.1", 9001)


def datagram(**overrides) -> bytes:
    msg = {"backend": "127.0.0.1:9001", "started_at": 100.0, "seq": 1,
           "cpu_percent": 12.5, "memory_percent": 3.0, "active_connections": 4}
    msg.update(overrides)
    return json.dumps(msg).encode()


def test_parse_valid_heartbeat():
    assert parse_heartbeat(datagram()) == (B, 100.0, 1, 12.5, 3.0, 4)


@pytest.mark.parametrize("data", [
    b"not json", b"[1, 2]", datagram(backend="nope"), datagram(cpu_percent=150),
    datagram(memory_percent=-1), datagram(active_connections=-2), datagram(seq=1.5),
    datagram(cpu_percent=True), datagram(started_at="yesterday"), b"\xff\xfe",
])
def test_parse_rejects_bad_datagrams(data):
    with pytest.raises(ValueError):
        parse_heartbeat(data)


@pytest.fixture
def listener():
    return HeartbeatListener(Router([B], RoundRobinStrategy()), port=0)


def test_heartbeat_updates_stats(listener):
    assert listener.handle(datagram(), received_at=50.0)
    stats = listener.router.stats.get(B)
    assert (stats.cpu_percent, stats.memory_percent, stats.reported_connections, stats.heartbeat_at) == \
        (12.5, 3.0, 4, 50.0)


def test_duplicate_and_out_of_order_datagrams_are_dropped(listener):
    assert listener.handle(datagram(seq=5, cpu_percent=50), 1.0)
    assert not listener.handle(datagram(seq=5, cpu_percent=60), 2.0)  # duplicate
    assert not listener.handle(datagram(seq=4, cpu_percent=70), 3.0)  # older, arrived late
    assert listener.router.stats.get(B).cpu_percent == 50
    assert listener.handle(datagram(seq=6, cpu_percent=80), 4.0)


def test_a_restarted_agent_is_accepted_despite_a_lower_seq(listener):
    listener.handle(datagram(started_at=100.0, seq=500), 1.0)
    assert listener.handle(datagram(started_at=200.0, seq=1, cpu_percent=1.0), 2.0)
    assert listener.router.stats.get(B).cpu_percent == 1.0


def test_heartbeats_for_unknown_backends_are_ignored(listener):
    assert not listener.handle(datagram(backend="10.9.9.9:80"), 1.0)
    assert Backend("10.9.9.9", 80) not in listener.router.stats.snapshot()


def test_real_agent_to_listener_over_udp(listener):
    host, port = listener.start()
    agent = HeartbeatAgent([((host, port), "127.0.0.1:9001")], active_connections=lambda: 7, interval=0.05)
    agent.start()
    try:
        deadline = time.monotonic() + 3
        while listener.accepted < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        stats = listener.router.stats.get(B)
        assert listener.accepted >= 2
        assert stats.reported_connections == 7
        assert 0 <= stats.cpu_percent <= 100 and 0 < stats.memory_percent <= 100
    finally:
        agent.stop()
        listener.stop()


def test_listener_survives_garbage_on_its_port(listener):
    host, port = listener.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"\x00garbage", (host, port))
            s.sendto(datagram(), (host, port))
        deadline = time.monotonic() + 2
        while listener.accepted < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (listener.dropped, listener.accepted) == (1, 1)
    finally:
        listener.stop()


def test_backend_counts_in_flight_requests():
    server = start_backend("a", delay_ms=300)
    try:
        t = threading.Thread(target=request, args=(server.server_address[1],))
        t.start()
        time.sleep(0.1)
        assert server.in_flight() == 1
        t.join()
        assert server.in_flight() == 0
    finally:
        server.shutdown()
        server.server_close()
