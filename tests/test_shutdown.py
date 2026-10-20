"""Graceful shutdown: stop accepting, finish in-flight requests, give up after a timeout."""

import logging
import signal
import subprocess
import sys
import threading
import time

import pytest

from lb.config import PROJECT_ROOT, ConcurrencySettings
from lb.router import Router
from lb import server as server_module
from lb.server import LoadBalancer
from lb.strategies.round_robin import RoundRobinStrategy
from tests.test_proxy import as_backend, request, start_backend, unused_port


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


def client_in_thread(port) -> tuple[threading.Thread, list]:
    result = []

    def run():
        try:
            result.append(request(port)[0])
        except Exception as exc:
            result.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


@pytest.fixture
def slow_backend():
    server = start_backend("slow", delay_ms=500)
    yield server
    server.shutdown()
    server.server_close()


def start_lb(backend, **kwargs):
    lb = LoadBalancer(Router([as_backend(backend)], RoundRobinStrategy()), "127.0.0.1", 0, **kwargs)
    _, port = lb.start()
    serving = threading.Thread(target=lb.serve_forever, daemon=True)
    serving.start()
    return lb, port, serving


@pytest.mark.parametrize("thread_per_connection", [False, True], ids=["pool", "thread-per-connection"])
def test_in_flight_request_finishes_then_new_connections_are_refused(slow_backend, thread_per_connection):
    lb, port, serving = start_lb(slow_backend, thread_per_connection=thread_per_connection)
    client, result = client_in_thread(port)
    backend = lb.router.backends[0]
    wait_until(lambda: lb.router.stats.get(backend).active_connections == 1)

    lb.shutdown()
    serving.join(5)
    client.join(5)
    assert not serving.is_alive(), "serve_forever() did not return"
    assert result == [200], "the in-flight request was cut off"
    with pytest.raises(ConnectionRefusedError):
        request(port)


def test_queued_connections_are_served_before_exit(slow_backend):
    """One worker, three clients: one in flight and two already accepted into
    the queue when shutdown starts. All three were accepted, so all three are served."""
    lb, port, serving = start_lb(slow_backend, concurrency=ConcurrencySettings(workers=1))
    clients = [client_in_thread(port) for _ in range(3)]
    wait_until(lambda: len(lb._queue) == 2)

    lb.shutdown()
    serving.join(10)
    for thread, _ in clients:
        thread.join(5)
    assert not serving.is_alive()
    assert [result for _, result in clients] == [[200], [200], [200]]


def test_connection_waiting_for_a_full_queue_gets_503_on_shutdown():
    """Regression: the listener held an accepted connection it couldn't queue
    and, on shutdown, closed it with no response at all."""
    slow = start_backend("slow", delay_ms=1000)
    lb, port, serving = start_lb(slow, concurrency=ConcurrencySettings(workers=1, queue_size=1))
    try:
        first = client_in_thread(port)
        wait_until(lambda: lb.router.stats.get(lb.router.backends[0]).active_connections == 1)
        second = client_in_thread(port)
        wait_until(lambda: len(lb._queue) == 1)
        third = client_in_thread(port)  # accepted, but the listener can't queue it
        time.sleep(0.1)

        lb.shutdown()
        serving.join(10)
        for thread, _ in (first, second, third):
            thread.join(5)
        assert [first[1], second[1], third[1]] == [[200], [200], [503]]
    finally:
        slow.shutdown()
        slow.server_close()


def test_failing_to_start_a_worker_cleans_up(slow_backend, monkeypatch):
    """Regression: a "can't start new thread" while creating the pool escaped
    serve_forever with the listening socket still open and workers stranded."""
    started = []
    real_thread = threading.Thread

    class FailThirdWorker(real_thread):
        def start(self):
            if len(started) == 2:
                raise RuntimeError("can't start new thread")
            started.append(self)
            super().start()

    lb = LoadBalancer(Router([as_backend(slow_backend)], RoundRobinStrategy()), "127.0.0.1", 0,
                      concurrency=ConcurrencySettings(workers=4))
    _, port = lb.start()
    monkeypatch.setattr(server_module.threading, "Thread", FailThirdWorker)
    with pytest.raises(RuntimeError):
        lb.serve_forever()
    monkeypatch.undo()
    for worker in started:
        worker.join(2)
    assert not any(w.is_alive() for w in started), "started workers were left blocked in get()"
    with pytest.raises(ConnectionRefusedError):
        request(port)


def test_shutdown_gives_up_after_the_timeout(caplog):
    stuck = start_backend("stuck", delay_ms=5000)
    lb, port, serving = start_lb(stuck, concurrency=ConcurrencySettings(shutdown_timeout=0.3))
    try:
        client_in_thread(port)
        backend = lb.router.backends[0]
        wait_until(lambda: lb.router.stats.get(backend).active_connections == 1)

        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="lb.server"):
            lb.shutdown()
            serving.join(5)
        elapsed = time.monotonic() - started
        assert not serving.is_alive()
        # Up to one accept-poll interval (0.5 s) to notice the flag, plus the 0.3 s
        # drain limit; far less than the 5 s the backend would take.
        assert elapsed < 1.5
        assert "abandoning 1 in-flight" in caplog.text
    finally:
        stuck.shutdown()
        stuck.server_close()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_signal_drains_in_flight_request_and_exits_cleanly(tmp_path, slow_backend, signum):
    """The real thing: an LB process gets a signal while a request is in flight."""
    port = unused_port()
    config = tmp_path / "lb.toml"
    config.write_text(f'backends = ["127.0.0.1:{slow_backend.server_address[1]}"]\n'
                      f'[listen]\nport = {port}\n')
    proc = subprocess.Popen(
        [sys.executable, "-m", "lb.server", "--config", str(config)],
        cwd=PROJECT_ROOT, stderr=subprocess.PIPE, text=True,
    )
    try:
        # The LB logs "listening on ..." once its socket is bound.
        for line in proc.stderr:
            if "listening on" in line:
                break
        client, result = client_in_thread(port)
        time.sleep(0.2)  # the 500 ms backend request is now in flight

        proc.send_signal(signum)
        exit_code = proc.wait(5)
        client.join(5)
        rest = proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert result == [200]
    assert exit_code == 0
    assert "stopped" in rest


def test_second_signal_of_a_different_kind_force_kills(tmp_path):
    """SIGTERM starts a drain that is stuck on a 5 s backend; Ctrl+C must then
    kill at once. Regression: only the signal that arrived was reset, so SIGINT
    after SIGTERM just re-ran the graceful handler."""
    stuck = start_backend("stuck", delay_ms=5000)
    port = unused_port()
    config = tmp_path / "lb.toml"
    config.write_text(f'backends = ["127.0.0.1:{stuck.server_address[1]}"]\n'
                      f'[listen]\nport = {port}\n[concurrency]\nshutdown_timeout = 30\n')
    proc = subprocess.Popen(
        [sys.executable, "-m", "lb.server", "--config", str(config)],
        cwd=PROJECT_ROOT, stderr=subprocess.PIPE, text=True,
    )
    try:
        for line in proc.stderr:
            if "listening on" in line:
                break
        client_in_thread(port)
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        started = time.monotonic()
        proc.send_signal(signal.SIGINT)
        exit_code = proc.wait(5)
        assert time.monotonic() - started < 2
        assert exit_code == -signal.SIGINT  # killed by the signal's default action
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        stuck.shutdown()
        stuck.server_close()
