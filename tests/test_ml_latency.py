"""ML latency strategy: lowest prediction wins, only healthy backends, and every fallback path."""

import time

import joblib
import numpy as np

from lb.config import BreakerSettings, MLLatencySettings
from lb.router import Router
from lb.strategies.base import Backend
from lb.strategies.ml_latency import FEATURES, MLLatencyStrategy
from tests.test_round_robin import REQUEST

A, B, C = (Backend("127.0.0.1", p) for p in (9001, 9002, 9003))


class FakeModel:
    """predict() = the recent-average feature, or a callable for special cases."""

    def __init__(self, fn=None):
        self.fn = fn or (lambda X: X[:, FEATURES.index("recent_avg_latency_ms")])
        self.calls = 0

    def predict(self, X):
        self.calls += 1
        return self.fn(np.asarray(X))


def real_model():
    """A picklable scikit-learn model: latency = the recent-average feature."""
    from sklearn.linear_model import LinearRegression
    X = np.array([[0, 1, 1, ms] for ms in (10.0, 20.0, 30.0)])
    return LinearRegression().fit(X, X[:, 3])


def make(model=None, budget_ms=50.0, avg=(30.0, 10.0, 20.0), heartbeat=True):
    strategy = MLLatencyStrategy(model=model or FakeModel(), time_budget_ms=budget_ms)
    router = Router([A, B, C], strategy)
    for backend, ms in zip((A, B, C), avg):
        if heartbeat:
            router.stats.record_heartbeat(backend, 1.0, 1.0, 0, time.monotonic())
        if ms is not None:
            router.stats.request_started(backend)
            router.stats.request_finished(backend, ms)
    return strategy, router


def busy(router, backend, n):
    for _ in range(n):
        router.stats.request_started(backend)


def test_picks_the_lowest_predicted_latency():
    _, router = make()
    assert router.choose(REQUEST) == B


def test_features_are_passed_in_the_trained_order():
    seen = []
    _, router = make(FakeModel(lambda X: seen.append(X.copy()) or X[:, 0]))
    busy(router, C, 4)
    router.choose(REQUEST)
    assert seen[0].tolist() == [[0, 1.0, 1.0, 30.0], [0, 1.0, 1.0, 10.0], [4, 1.0, 1.0, 20.0]]


def test_only_picks_healthy_backends():
    _, router = make()  # B has the lowest prediction...
    router.set_healthy(B, False)  # ...but is down
    assert {router.choose(REQUEST) for _ in range(20)} == {C}
    router.set_healthy(C, False)
    assert {router.choose(REQUEST) for _ in range(20)} == {A}


def test_only_picks_backends_whose_circuit_is_closed():
    strategy = MLLatencyStrategy(model=FakeModel(), time_budget_ms=50)
    router = Router([A, B, C], strategy, BreakerSettings(failure_threshold=1, cooldown=60))
    for backend, ms in zip((A, B, C), (30.0, 10.0, 20.0)):
        router.stats.record_heartbeat(backend, 1.0, 1.0, 0, time.monotonic())
        router.stats.request_started(backend)
        router.stats.request_finished(backend, ms)
    lease = router.acquire(REQUEST)
    assert lease.backend == B
    router.release(lease, False)  # B fails once -> its circuit opens
    assert {router.choose(REQUEST) for _ in range(20)} == {C}


def test_exact_ties_are_spread_by_least_connections():
    _, router = make(FakeModel(lambda X: np.zeros(len(X))))
    assert [router.choose(REQUEST) for _ in range(3)] == [A, B, C]


def test_missing_model_falls_back_to_least_connections(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        strategy = MLLatencyStrategy(tmp_path / "missing.joblib")
        router = Router([A, B, C], strategy)
        busy(router, A, 2)
        busy(router, B, 1)
        assert router.choose(REQUEST) == C
    assert "cannot load model" in caplog.text
    assert "ml_latency fallback to least connections #1: no model loaded" in caplog.text
    assert strategy.fallbacks == 1


def test_model_with_other_features_is_refused(tmp_path, caplog):
    path = tmp_path / "old.joblib"
    joblib.dump({"model": real_model(), "features": ["active_connections"]}, path)
    with caplog.at_level("WARNING"):
        strategy = MLLatencyStrategy(path)
    assert strategy.model is None
    assert "expected" in caplog.text


def test_saved_model_is_loaded(tmp_path):
    path = tmp_path / "m.joblib"
    joblib.dump({"model": real_model(), "features": list(FEATURES), "name": "linear", "test_mae_ms": 1.0}, path)
    strategy = MLLatencyStrategy(path, time_budget_ms=50)
    router = Router([A, B, C], strategy)
    for backend, ms in zip((A, B, C), (30.0, 10.0, 20.0)):
        router.stats.record_heartbeat(backend, 1.0, 1.0, 0, time.monotonic())
        router.stats.request_started(backend)
        router.stats.request_finished(backend, ms)
    assert strategy.model_info["name"] == "linear"
    assert router.choose(REQUEST) == B  # the real model, end to end
    assert strategy.fallbacks == 0


def test_prediction_error_falls_back(caplog):
    def explode(X):
        raise ValueError("bad input")
    strategy, router = make(FakeModel(explode))
    busy(router, B, 3)
    with caplog.at_level("WARNING"):
        assert router.choose(REQUEST) in (A, C)  # least connections: B is the busy one
    assert "error: ValueError('bad input')" in caplog.text
    assert strategy.fallbacks == 1


def test_non_finite_prediction_falls_back():
    strategy, router = make(FakeModel(lambda X: np.array([np.nan, 1.0, 2.0])))
    router.choose(REQUEST)
    assert strategy.fallbacks == 1


def test_slow_prediction_over_the_budget_falls_back(caplog):
    def slow(X):
        time.sleep(0.02)
        return X[:, 3]
    strategy, router = make(FakeModel(slow), budget_ms=5.0)
    busy(router, B, 3)  # the model would pick B; least connections must not
    with caplog.at_level("WARNING"):
        assert router.choose(REQUEST) in (A, C)
    assert strategy.fallbacks == 1
    assert "budget 5 ms" in caplog.text


def test_missing_heartbeat_falls_back():
    strategy, router = make(heartbeat=False)
    router.choose(REQUEST)
    assert strategy.fallbacks == 1


def test_stale_heartbeat_falls_back():
    strategy, router = make(heartbeat=False)
    for backend in (A, B, C):
        router.stats.record_heartbeat(backend, 1.0, 1.0, 0, time.monotonic() - 10)
    router.choose(REQUEST)
    assert strategy.fallbacks == 1


def test_backend_without_latency_history_falls_back():
    strategy, router = make(avg=(30.0, None, 20.0))
    router.choose(REQUEST)
    assert strategy.fallbacks == 1


def test_from_config_resolves_the_model_path_against_the_repo(monkeypatch):
    from lb.config import PROJECT_ROOT
    seen = {}
    monkeypatch.setattr("lb.strategies.ml_latency.load_model", lambda p: seen.setdefault("path", p) and {})

    class Cfg:
        ml_latency = MLLatencySettings(model_path="ml/models/x.joblib", time_budget_ms=3.0)
    strategy = MLLatencyStrategy.from_config(Cfg())
    assert seen["path"] == PROJECT_ROOT / "ml/models/x.joblib"
    assert strategy.time_budget_ms == 3.0


def test_a_fast_model_stays_within_the_default_budget():
    strategy, router = make(budget_ms=MLLatencySettings().time_budget_ms)
    for _ in range(200):
        router.choose(REQUEST)
    assert strategy.fallbacks <= 2  # a rare OS scheduling hiccup is allowed, a pattern is not
