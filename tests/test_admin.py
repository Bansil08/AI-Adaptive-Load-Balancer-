"""Admin API: list, add, remove with draining; plus sticky sessions and router leases."""

import http.client
import json
import threading
import time

import pytest

from lb.admin import AdminServer
from lb.config import AdminSettings, HealthSettings
from lb.health import HealthChecker
from lb.router import NoBackendAvailable, Router, sticky_choice
from lb.strategies.base import Backend
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_proxy import as_backend, request, start_backend, start_lb
from tests.test_round_robin import REQUEST
from tests.test_shutdown import client_in_thread, wait_until

A, B, C = (Backend("127.0.0.1", p) for p in (9001, 9002, 9003))


def admin_call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def test_list_add_and_remove_via_dispatch():
    router = Router([A], RoundRobinStrategy())
    admin = AdminServer(router)
    status, payload = admin.dispatch("GET", "/backends", b"")
    assert status == 200 and [b["address"] for b in payload["backends"]] == ["127.0.0.1:9001"]
    assert payload["strategy"] == "round_robin" and "fallbacks" not in payload

    assert admin.dispatch("POST", "/backends", b'{"address": "127.0.0.1:9002"}')[0] == 201
    assert router.backends == (A, B)
    assert admin.dispatch("POST", "/backends", b'{"address": "127.0.0.1:9002"}')[0] == 409
    assert admin.dispatch("POST", "/backends", b'{"addr": 1}')[0] == 400

    status, payload = admin.dispatch("DELETE", "/backends/127.0.0.1:9001", b"")
    assert status == 200 and payload["drained"] is True
    assert router.backends == (B,)
    assert admin.dispatch("DELETE", "/backends/127.0.0.1:9001", b"")[0] == 404


@pytest.mark.parametrize("method, path, status", [
    ("GET", "/nope", 404), ("PUT", "/backends", 405), ("GET", "/backends/127.0.0.1:9001", 405),
    ("DELETE", "/backends/not-an-address", 400),
])
def test_bad_admin_requests(method, path, status):
    assert AdminServer(Router([A], RoundRobinStrategy())).dispatch(method, path, b"")[0] == status


def test_added_backend_waits_for_health_checks_when_a_checker_runs():
    router = Router([A], RoundRobinStrategy())
    admin = AdminServer(router, health=HealthChecker(router, HealthSettings()))
    status, payload = admin.dispatch("POST", "/backends", b'{"address": "127.0.0.1:9002"}')
    assert status == 201 and payload["healthy"] is False
    assert {router.choose(REQUEST) for _ in range(4)} == {A}


def test_remove_drains_in_flight_requests_first():
    """A request is in flight on the slow backend; DELETE must wait for it to
    finish (and it must succeed), while new requests go elsewhere at once."""
    slow = start_backend("slow", delay_ms=600)
    fast = start_backend("fast")
    lb, port = start_lb([as_backend(slow), as_backend(fast)])
    admin = AdminServer(lb.router, AdminSettings(port=0, drain_timeout=5))
    _, admin_port = admin.start()
    try:
        client, result = client_in_thread(port)  # round robin: goes to slow
        wait_until(lambda: lb.router.stats.get(as_backend(slow)).active_connections == 1)

        removal = {}
        remover = threading.Thread(target=lambda: removal.update(
            reply=admin_call(admin_port, "DELETE", f"/backends/{as_backend(slow)}")))
        started = time.monotonic()
        remover.start()
        time.sleep(0.1)
        # While draining: still listed as draining, and new requests avoid it.
        listed = admin_call(admin_port, "GET", "/backends")[1]["backends"]
        assert [r["draining"] for r in listed] == [True, False]
        assert json.loads(request(port)[2])["server"] == "fast"

        remover.join(5)
        client.join(5)
        assert result == [200]  # the in-flight request was not cut off
        status, payload = removal["reply"]
        assert status == 200 and payload["drained"] is True
        assert time.monotonic() - started >= 0.4  # it really waited for the request
        assert lb.router.backends == (as_backend(fast),)
    finally:
        admin.stop()
        lb.shutdown()
        for s in (slow, fast):
            s.shutdown()
            s.server_close()


def test_drain_gives_up_after_its_timeout():
    router = Router([A], RoundRobinStrategy())
    router.acquire(REQUEST)  # a lease that is never released
    started = time.monotonic()
    drained, left = router.drain_and_remove(A, timeout=0.2)
    assert (drained, left) == (False, 1)
    assert 0.2 <= time.monotonic() - started < 1
    assert router.backends == ()


def test_draining_backend_gets_no_new_leases():
    router = Router([A, B], RoundRobinStrategy())
    held = router.acquire(REQUEST)  # holds A
    remover = threading.Thread(target=router.drain_and_remove, args=(A, 5))
    remover.start()
    wait_until(lambda: router.views()[0].draining)
    assert {router.acquire(REQUEST).backend for _ in range(4)} == {B}
    router.release(held, success=True)
    remover.join(2)
    assert not remover.is_alive() and router.backends == (B,)


def test_admin_server_over_http():
    router = Router([A], RoundRobinStrategy())
    admin = AdminServer(router, AdminSettings(port=0))
    _, port = admin.start()
    try:
        assert admin_call(port, "POST", "/backends", {"address": "127.0.0.1:9002"})[0] == 201
        status, payload = admin_call(port, "GET", "/backends")
        assert status == 200 and len(payload["backends"]) == 2
    finally:
        admin.stop()


# --- sticky sessions --------------------------------------------------------------


def test_sticky_same_ip_always_gets_the_same_backend():
    router = Router([A, B, C], RoundRobinStrategy(), sticky_sessions=True)
    for ip in ("10.0.0.1", "10.0.0.2", "192.168.1.9"):
        assert len({router.choose(REQUEST, ip) for _ in range(10)}) == 1


def test_sticky_spreads_different_clients():
    ips = [f"10.0.{i // 256}.{i % 256}" for i in range(600)]
    counts = {b: 0 for b in (A, B, C)}
    for ip in ips:
        counts[sticky_choice([A, B, C], ip)] += 1
    assert all(150 < n < 250 for n in counts.values())  # roughly a third each


def test_sticky_only_clients_of_a_removed_backend_move():
    ips = [f"10.1.{i // 256}.{i % 256}" for i in range(300)]
    before = {ip: sticky_choice([A, B, C], ip) for ip in ips}
    after = {ip: sticky_choice([A, C], ip) for ip in ips}
    moved = [ip for ip in ips if before[ip] != after[ip]]
    assert moved and all(before[ip] == B for ip in moved)


def test_sticky_retry_excludes_the_failed_backend():
    router = Router([A, B, C], RoundRobinStrategy(), sticky_sessions=True)
    first = router.choose(REQUEST, "10.0.0.1")
    second = router.choose(REQUEST, "10.0.0.1", exclude=[first])
    assert second != first


def test_unhealthy_backends_are_never_chosen():
    router = Router([A, B], RoundRobinStrategy())
    router.set_healthy(A, False)
    assert {router.choose(REQUEST) for _ in range(6)} == {B}
    router.set_healthy(B, False)
    with pytest.raises(NoBackendAvailable):
        router.choose(REQUEST)


def test_old_lease_does_not_touch_a_re_added_backend():
    """Regression: leases were looked up by address, so a request left over
    from a forced removal decremented the re-added entry to -1 in flight, and
    a later drain of it would then stop waiting too early."""
    router = Router([A], RoundRobinStrategy())
    old = router.acquire(REQUEST)
    router.drain_and_remove(A, timeout=0.05)   # gives up: one request still running
    router.add_backend(A)
    new = router.acquire(REQUEST)
    router.release(old, success=True)
    assert router.views()[0].in_flight == 1     # only `new` is in flight
    remover = threading.Thread(target=router.drain_and_remove, args=(A, 5))
    remover.start()
    time.sleep(0.1)
    assert remover.is_alive(), "drain stopped waiting while a request was in flight"
    router.release(new, success=True)
    remover.join(2)
    assert not remover.is_alive()


def test_forced_removal_keeps_stats_consistent():
    """Regression: removing the stats entry let the leftover request recreate it at -1."""
    router = Router([A], RoundRobinStrategy())
    admin = AdminServer(router, AdminSettings(drain_timeout=0.05))
    lease = router.acquire(REQUEST)
    router.stats.request_started(A)
    assert admin.remove_backend(str(A))[1]["drained"] is False
    router.stats.request_finished(A, 5.0)
    router.release(lease, success=True)
    assert router.stats.get(A).active_connections == 0


def test_connection_returned_after_a_forced_removal_is_closed():
    """A request outlives a drain timeout; the connection it puts back must not stay pooled."""
    from lb.config import PoolSettings
    from lb.connection_pool import ConnectionPool
    from lb.proxy import Proxy
    from tests.test_retries import make_request
    slow = start_backend("slow", delay_ms=400)
    b = as_backend(slow)
    router = Router([b], RoundRobinStrategy())
    proxy = Proxy(router, pool=ConnectionPool(PoolSettings(size=4)))
    try:
        worker = threading.Thread(target=proxy.route_and_forward, args=(make_request("GET"), "1.1.1.1"))
        worker.start()
        wait_until(lambda: router.views()[0].in_flight == 1)
        assert router.drain_and_remove(b, timeout=0.05) == (False, 1)
        worker.join(5)
        assert proxy.pool.counters().idle == 0
    finally:
        slow.shutdown()
        slow.server_close()
