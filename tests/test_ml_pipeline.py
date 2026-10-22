"""ML pipeline: features rebuilt without leakage, time split, export from SQL, config, the proxy's observe hook."""

import json
import threading

import pytest

from lb.config import ProxySettings, load_lb_config
from lb.router import Router
from lb.server import LoadBalancer
from lb.stats import LATENCY_EWMA_ALPHA
from lb.strategies.base import Backend, LoadBalancingStrategy
from ml.export_data import export, replay_lb_counters
from ml.train import time_split
from tests.test_proxy import as_backend, request, start_backend, unused_port


def row(received_s, ms, status=200):
    return {"received_s": received_s, "response_time_ms": ms, "status_code": status}


def test_replay_counts_in_flight_requests_at_each_arrival():
    rows = [row(0.0, 1000), row(0.1, 100), row(0.5, 100), row(1.5, 10)]
    replay_lb_counters(rows)
    # At 0.5 the 1st (until 1.0) is still running, the 2nd finished at 0.2.
    assert [r["active_connections"] for r in rows] == [0, 1, 1, 0]


def test_replay_average_only_uses_requests_that_had_finished():
    # The 1st request arrived first but finishes last: it must not be in the
    # 2nd's average (that would be leakage: the LB could not know it yet).
    rows = [row(0.0, 5000), row(0.1, 100), row(0.3, 50), row(6.0, 10)]
    replay_lb_counters(rows)
    assert rows[0]["recent_avg_latency_ms"] is None
    assert rows[1]["recent_avg_latency_ms"] is None   # nothing finished by 0.1
    assert rows[2]["recent_avg_latency_ms"] == 100     # only the 2nd (done at 0.2)
    expected = 100
    for sample in (50, 5000):                         # finish order: 0.35, then 5.0
        expected = LATENCY_EWMA_ALPHA * sample + (1 - LATENCY_EWMA_ALPHA) * expected
    assert rows[3]["recent_avg_latency_ms"] == pytest.approx(expected)


def test_replay_skips_502s_in_the_average_like_the_lb():
    rows = [row(0.0, 1, status=502), row(0.1, 40), row(1.0, 10)]
    replay_lb_counters(rows)
    assert rows[2]["recent_avg_latency_ms"] == 40


def test_time_split_tests_on_the_end_of_every_run():
    rows = [{"run_id": run, "t_in_run_s": float(t)} for run in ("1", "2") for t in range(10)]
    train, test = time_split(rows, 0.3)
    for run in ("1", "2"):
        last_train = max(r["t_in_run_s"] for r in train if r["run_id"] == run)
        first_test = min(r["t_in_run_s"] for r in test if r["run_id"] == run)
        assert last_train < first_test  # never trained on anything later than a test row
    assert len(test) == 6


@pytest.mark.db
def test_export_joins_heartbeats_and_drops_stale_ones(db):
    server = db.execute("INSERT INTO servers (address, port) VALUES ('127.0.0.1', 9001) RETURNING id").fetchone()[0]
    run = db.execute("INSERT INTO experiment_runs (strategy, scenario, started_at, ended_at, notes)"
                     " VALUES ('round_robin', 'x', now() - interval '20 seconds', now(), %s) RETURNING id",
                     (json.dumps({"batch": "b1"}),)).fetchone()[0]
    with db.transaction():
        db.execute("INSERT INTO server_metrics (server_id, recorded_at, cpu_percent, memory_percent,"
                   " active_connections) VALUES (%s, now() - interval '15 seconds', 5, 1, 0)", (server,))
        for ago, ms in (("14 seconds", 30.0), ("5 seconds", 40.0)):  # 1 s and 10 s after the heartbeat
            db.execute("INSERT INTO requests (run_id, server_id, received_at, method, path, status_code,"
                       " response_time_ms) VALUES (%s, %s, now() - %s::interval, 'GET', '/', 200, %s)",
                       (run, server, ago, ms))
    rows = export(db, "b1")
    assert [(r["cpu_percent"], r["active_connections"], r["recent_avg_latency_ms"]) for r in rows] == [
        (5.0, 0, None),     # fresh heartbeat; nothing finished yet
        (None, 0, 30.0),    # heartbeat 10 s old: stale, so unknown (as live)
    ]
    assert export(db, "other-batch") == []


@pytest.mark.db
def test_export_ignores_a_heartbeat_from_the_previous_run(db):
    # Runs reuse the same backend ports (same servers row). A heartbeat the
    # previous run's backend sent 1 s before this run's first request is
    # "the latest before it" in SQL, but this run's fresh LB never received it.
    server = db.execute("INSERT INTO servers (address, port) VALUES ('127.0.0.1', 9001) RETURNING id").fetchone()[0]
    notes = json.dumps({"batch": "b"})
    with db.transaction():
        db.execute("INSERT INTO experiment_runs (strategy, scenario, started_at, ended_at, notes)"
                   " VALUES ('round_robin', 'x', now() - interval '60 seconds', now() - interval '20.5 seconds', %s)",
                   (notes,))
        db.execute("INSERT INTO server_metrics (server_id, recorded_at, cpu_percent, memory_percent,"
                   " active_connections) VALUES (%s, now() - interval '21 seconds', 5, 1, 0)", (server,))
        run = db.execute("INSERT INTO experiment_runs (strategy, scenario, started_at, ended_at, notes)"
                         " VALUES ('round_robin', 'x', now() - interval '20 seconds', now(), %s) RETURNING id",
                         (notes,)).fetchone()[0]
        db.execute("INSERT INTO requests (run_id, server_id, received_at, method, path, status_code,"
                   " response_time_ms) VALUES (%s, %s, now() - interval '20 seconds', 'GET', '/', 200, 30)",
                   (run, server))
    (row,) = export(db, "b")
    assert row["metrics_age_ms"] == pytest.approx(1000) and row["cpu_percent"] is None


def test_ml_config_sections_are_read(tmp_path):
    path = tmp_path / "lb.toml"
    path.write_text('strategy = "bandit"\n[bandit]\nepsilon = 0.2\nwindow = 10\nseed = 7\n'
                    '[ml_latency]\nmodel_path = "/tmp/m.joblib"\ntime_budget_ms = 1.5\n')
    cfg = load_lb_config(path)
    assert (cfg.bandit.epsilon, cfg.bandit.window, cfg.bandit.seed) == (0.2, 10, 7)
    assert (cfg.ml_latency.model_path, cfg.ml_latency.time_budget_ms) == ("/tmp/m.joblib", 1.5)
    from lb.strategies import STRATEGIES
    strategy = STRATEGIES[cfg.strategy].from_config(cfg)
    assert (strategy.epsilon, strategy.window) == (0.2, 10)


@pytest.mark.parametrize("content", [
    "[bandit]\nepsilon = 1.5", "[bandit]\nepsilon = -0.1", "[bandit]\nwindow = 0",
    "[bandit]\nfailure_penalty_ms = -1", "[ml_latency]\ntime_budget_ms = 0", "[ml_latency]\nmodel = 'x'",
])
def test_bad_ml_values_raise_value_error(tmp_path, content):
    path = tmp_path / "lb.toml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_lb_config(path)


class Recorder(LoadBalancingStrategy):
    """Picks backends in order and records what the proxy reports back."""

    name = "recorder"

    def __init__(self):
        self.observed = []
        self.i = 0

    def choose(self, backends, request):
        self.i += 1
        return backends[(self.i - 1) % len(backends)]

    def observe(self, backend, latency_ms):
        self.observed.append((backend, latency_ms))


def test_proxy_reports_each_outcome_to_the_strategy():
    good = start_backend("good", delay_ms=20)
    dead = Backend("127.0.0.1", unused_port())
    strategy = Recorder()
    lb = LoadBalancer(Router([as_backend(good), dead], strategy), "127.0.0.1", 0, ProxySettings())
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        assert [request(port)[0] for _ in range(2)] == [200, 502]
        (b1, ms), (b2, failed) = strategy.observed
        assert b1 == as_backend(good) and ms >= 20
        assert b2 == dead and failed is None  # a failure is reported as None, never as a fast answer
    finally:
        lb.shutdown()
        good.shutdown()
        good.server_close()


def test_a_broken_observe_does_not_fail_the_request():
    good = start_backend("good")

    class Broken(Recorder):
        def observe(self, backend, latency_ms):
            raise RuntimeError("bug")
    lb = LoadBalancer(Router([as_backend(good)], Broken()), "127.0.0.1", 0, ProxySettings())
    _, port = lb.start()
    threading.Thread(target=lb.serve_forever, daemon=True).start()
    try:
        assert [request(port)[0] for _ in range(2)] == [200, 200]
        assert lb.router.views()[0].in_flight == 0  # the lease was still released
    finally:
        lb.shutdown()
        good.shutdown()
        good.server_close()
