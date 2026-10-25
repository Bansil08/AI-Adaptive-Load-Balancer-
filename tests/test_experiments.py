"""Phase 5 building blocks: backend capacity/control, load generator, scenarios, run_results SQL."""

import http.client
import json
import threading
import time

import pytest

from backend.app import BackendServer
from experiments.load_generator import LoadGenerator, schedule, step_profile, summarise
from experiments.run_experiments import RUN_RESULTS_SQL, is_full_batch
from experiments.scenarios import SCENARIOS, BackendSpec
from tests.test_proxy import request


def serve(server: BackendServer) -> BackendServer:
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.fixture
def backend_factory():
    servers = []

    def make(**kwargs):
        server = serve(BackendServer(("127.0.0.1", 0), "t", quiet=True, **kwargs))
        servers.append(server)
        return server
    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


def post(port, path, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=json.dumps(body))
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


# --- backend: capacity and control ---------------------------------------------------


def test_capacity_queues_requests_beyond_n(backend_factory):
    """Capacity 2, 200 ms each, 4 at once: two run, two wait, so the batch takes ~400 ms."""
    server = backend_factory(delay_ms=200, capacity=2)
    port = server.server_address[1]
    threads = [threading.Thread(target=request, args=(port,)) for _ in range(4)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 0.38 < time.monotonic() - started < 0.7


def test_unlimited_capacity_runs_everything_at_once(backend_factory):
    port = backend_factory(delay_ms=200).server_address[1]
    threads = [threading.Thread(target=request, args=(port,)) for _ in range(4)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert time.monotonic() - started < 0.35


def test_health_skips_the_capacity_queue(backend_factory):
    server = backend_factory(delay_ms=500, capacity=1)
    port = server.server_address[1]
    busy = threading.Thread(target=request, args=(port,))
    busy.start()
    time.sleep(0.05)
    started = time.monotonic()
    assert request(port, path="/health")[0] == 200
    assert time.monotonic() - started < 0.2
    busy.join()


def test_control_endpoint_is_off_by_default(backend_factory):
    port = backend_factory(delay_ms=10).server_address[1]
    assert post(port, "/control", {"delay_ms": 999})[0] == 404


def test_control_endpoint_changes_the_delay(backend_factory):
    server = backend_factory(delay_ms=10, allow_control=True)
    port = server.server_address[1]
    status, payload = post(port, "/control", {"delay_ms": 250})
    assert status == 200 and payload["delay_ms"] == 250
    started = time.monotonic()
    request(port)
    assert time.monotonic() - started >= 0.25


@pytest.mark.parametrize("body", [{"delay_ms": -1}, {"delay_ms": "slow"}, {"delay_ms": 10 ** 9}, [1, 2]])
def test_control_endpoint_rejects_bad_values(backend_factory, body):
    server = backend_factory(delay_ms=10, allow_control=True)
    assert post(server.server_address[1], "/control", body)[0] == 400
    assert server.delay_ms == 10


def test_seed_makes_jitter_repeatable():
    a = [BackendServer(("127.0.0.1", 0), "x", jitter_ms=50, seed=7) for _ in range(2)]
    try:
        assert [a[0].rng.uniform(0, 50) for _ in range(5)] == [a[1].rng.uniform(0, 50) for _ in range(5)]
    finally:
        for s in a:
            s.server_close()


# --- load generator -----------------------------------------------------------------------


def test_schedule_is_reproducible_and_matches_the_rate():
    profile = step_profile([(0, 100)])
    first, second = schedule(profile, 20, seed=1), schedule(profile, 20, seed=1)
    assert first == second                          # same seed, same arrivals
    assert schedule(profile, 20, seed=2) != first   # a different seed differs
    assert 1850 < len(first) < 2150                 # Poisson: ~100/s * 20 s, within ~3.5 sigma
    assert all(0 <= t < 20 for t in first) and first == sorted(first)


def test_step_profile_changes_rate_at_the_step():
    times = schedule(step_profile([(0, 20), (10, 200)]), 20, seed=3)
    before = sum(1 for t in times if t < 10)
    after = len(times) - before
    assert 140 < before < 260 and 1800 < after < 2200


def test_load_generator_sends_open_loop_and_records_results(backend_factory):
    """Each request takes 300 ms, the rate is 20/s for 1 s: an open-loop
    generator still sends ~20 (a closed loop with one client would send ~4)."""
    port = backend_factory(delay_ms=300).server_address[1]
    fired = []
    results, started_wall = LoadGenerator("127.0.0.1", port).run(
        step_profile([(0, 20)]), 1.0, seed=5, events=[(0.5, lambda: fired.append(time.monotonic()))])
    assert len(results) == len(schedule(step_profile([(0, 20)]), 1.0, seed=5))
    assert all(r.status == 200 and r.latency_ms >= 300 and r.backend == "t" for r in results)
    assert len(fired) == 1
    assert abs(started_wall - time.time()) < 5


def test_latency_is_measured_from_the_scheduled_time(backend_factory):
    """One worker, requests every ~50 ms, each takes 200 ms: requests queue on
    the client side, and that wait must show up in the latency (no coordinated omission)."""
    port = backend_factory(delay_ms=200).server_address[1]
    results, _ = LoadGenerator("127.0.0.1", port, workers=1).run(step_profile([(0, 20)]), 0.5, seed=1)
    assert len(results) >= 5
    latencies = [r.latency_ms for r in sorted(results, key=lambda r: r.scheduled_s)]
    assert latencies[-1] > latencies[0] + 300   # later ones waited behind earlier ones


def test_summarise_counts_errors_but_not_their_latency():
    from experiments.load_generator import Result
    results = [Result(0, 10.0, 200, None, "a"), Result(0, 20.0, 200, None, "a"),
               Result(0, 1.0, 502, None, None), Result(0, 5.0, None, "ConnectionRefusedError", None)]
    s = summarise(results, duration=2.0)
    assert (s["sent"], s["ok"], s["error_rate"], s["avg_latency_ms"], s["throughput_rps"]) == (4, 2, 0.5, 15.0, 1.0)


# --- scenarios ------------------------------------------------------------------------------


def test_scenarios_stress_what_their_descriptions_claim():
    slow = next(b for b in SCENARIOS["traffic_spike"].backends if b.name == "slow")
    assert slow.max_rps < 180 / 3                        # round robin's share of the spike overloads it
    assert slow.max_rps > 90 / 3                         # but not at the steady unequal rate
    assert BackendSpec("b", 300).max_rps < 90 / 3        # the slowed backend can't keep up with its share
    assert {e.action for s in SCENARIOS.values() for e in s.events} == {"kill", "set_delay"}


def test_scaling_moves_events_and_rate_steps():
    s = SCENARIOS["traffic_spike"].scaled(15)
    assert s.duration == 15 and s.rate_steps == ((0.0, 30), (5.0, 180))
    assert SCENARIOS["server_crash"].scaled(15).events[0].at == 7.5


# --- run_results SQL ---------------------------------------------------------------------------


@pytest.mark.db
def test_run_results_sql(db):
    run_id = db.execute("INSERT INTO experiment_runs (strategy, scenario) VALUES ('round_robin', 'x')"
                        " RETURNING id").fetchone()[0]
    rows = [(run_id, ms, 200) for ms in range(1, 21)] + [(run_id, 0.5, 502), (run_id, 0.5, 503)]
    with db.cursor() as cur:
        cur.executemany("INSERT INTO requests (run_id, method, path, status_code, response_time_ms)"
                        " VALUES (%s, 'GET', '/', %s, %s)", [(r, s, ms) for r, ms, s in rows])
    db.execute("INSERT INTO requests (method, path, status_code, response_time_ms) VALUES ('GET', '/', 200, 999)")
    avg, p50, p95, p99, error_rate, throughput = db.execute(
        RUN_RESULTS_SQL, {"run_id": run_id, "duration": 10}).fetchone()
    assert avg == pytest.approx(10.5)        # the two fast errors and the untagged row are excluded
    # 1..20 ms, linear interpolation, all three from one percentile_cont call: 1 + p * 19
    assert (p50, p95, p99) == (pytest.approx(10.5), pytest.approx(19.05), pytest.approx(19.81))
    assert error_rate == pytest.approx(2 / 22)
    assert throughput == pytest.approx(2.0)  # 20 successful / 10 s
    assert db.execute("SELECT count(*) FROM run_results WHERE run_id = %s", (run_id,)).fetchone()[0] == 1



@pytest.mark.db
def test_throughput_counts_the_time_a_backlog_takes_to_drain(db):
    """Regression: throughput divided by the planned duration, so a run still
    serving its backlog at 34 s looked as fast as one done at 30 s."""
    run_id = db.execute("INSERT INTO experiment_runs (strategy, scenario, started_at) VALUES ('rr', 'x',"
                        " now() - interval '20 seconds') RETURNING id").fetchone()[0]
    with db.cursor() as cur:  # 20 requests, the last one finishing 15 s after the start
        cur.executemany("INSERT INTO requests (run_id, received_at, method, path, status_code, response_time_ms)"
                        " VALUES (%s, now() - interval '20 seconds' + %s * interval '1 second', 'GET', '/', 200, %s)",
                        [(run_id, i * 0.5, 5000.0 if i == 19 else 10.0) for i in range(20)])
    throughput = db.execute(RUN_RESULTS_SQL, {"run_id": run_id, "duration": 10}).fetchone()[5]
    assert throughput == pytest.approx(20 / 14.5, rel=0.01)   # last response at 9.5 s + 5 s, not 20 / 10


def test_a_failing_event_still_releases_the_worker_threads(backend_factory):
    """Regression: an exception in an event callback skipped the shutdown of
    the worker pool, leaving its threads waiting on the queue forever."""
    port = backend_factory(delay_ms=5).server_address[1]
    before = threading.active_count()

    def broken():
        raise RuntimeError("backend did not answer /control")

    with pytest.raises(RuntimeError):
        LoadGenerator("127.0.0.1", port, workers=16).run(step_profile([(0, 20)]), 0.5, events=[(0.2, broken)])
    time.sleep(0.1)
    assert threading.active_count() <= before


def test_only_a_full_batch_may_replace_the_committed_reports():
    full = [{"scenario": name, "duration": s.duration} for name, s in SCENARIOS.items()]
    assert is_full_batch(full, repeats=3)
    assert not is_full_batch(full, repeats=1)                                  # too few repeats
    assert not is_full_batch(full[:1], repeats=3)                              # one scenario
    assert not is_full_batch([dict(r, duration=12.0) for r in full], repeats=3)  # shortened runs


def test_set_delay_sends_the_admin_token_the_backends_expect(monkeypatch):
    # Regression: with ADMIN_TOKEN in .env the experiment backends inherit it and
    # require it, and the sudden-slowdown event failed with 403.
    from experiments.run_experiments import set_delay
    monkeypatch.setenv("ADMIN_TOKEN", "tok")
    server = BackendServer(("127.0.0.1", 0), "b", delay_ms=30, quiet=True, allow_control=True, control_token="tok")
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        set_delay(server.server_address[1], 300)
        assert server.delay_ms == 300
    finally:
        server.shutdown()
        server.server_close()


def test_static_page_assets_match_the_dashboards():
    # docs/assets/ holds copies (experiments/export_static.py makes them); if they
    # drift, the published page draws differently from the dashboard. Run `make static`.
    from lb.config import PROJECT_ROOT
    for name in ("results.js", "dashboard.css"):
        assert (PROJECT_ROOT / "docs" / "assets" / name).read_bytes() == \
            (PROJECT_ROOT / "dashboard" / "static" / name).read_bytes(), f"docs/assets/{name} is stale: make static"
