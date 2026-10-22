"""Supervised strategy: route to the backend with the lowest predicted latency.

The idea in plain words: from past requests we know, for each one, what the
backend looked like when the request was sent (how many requests it already
had, its CPU and memory, how fast its recent answers were) and how long the
request then took. ml/train.py fits a regression model to those examples,
i.e. a function  f(state of a backend) -> expected response time in ms.
Here, for every request, we describe each healthy backend's CURRENT state,
ask the model for its expected latency, and pick the smallest.

The four features (FEATURES below), all read from the live ServerStats:
  - active_connections:    requests this LB has in flight to the backend
  - cpu_percent:           from the backend's latest UDP heartbeat
  - memory_percent:        from the same heartbeat
  - recent_avg_latency_ms: EWMA of its recent response times (lb/stats.py)
ml/export_data.py rebuilds exactly these values for past requests from the
request log, using the same definitions (it imports FEATURES,
MAX_HEARTBEAT_AGE_S and the EWMA weight from here and lb/stats.py). If the
two sides computed a feature differently, the model would be fed inputs
unlike the ones it learned from ("training/serving skew").

When it cannot predict (no model file, a backend without a fresh heartbeat
or without any latency history yet), it raises NoPrediction and the request
goes to least connections instead (lb/strategies/guarded.py), as it also
does when the prediction throws or takes longer than the time budget.

Exact ties (common with trees, which predict one value per leaf) are broken
by least connections too, so two equal predictions don't send everything to
the backend that happens to be listed first.

Security note: the model file is a pickle (joblib). Unpickling runs code, so
only ever load a model file this project trained itself.
"""

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from lb.strategies.base import Backend
from lb.strategies.guarded import GuardedStrategy, NoPrediction

if TYPE_CHECKING:
    from lb.config import LBConfig
    from lb.http_parser import HTTPRequest
    from lb.stats import BackendStats

log = logging.getLogger(__name__)

# Order matters: it is the column order of the training matrix.
FEATURES = ("active_connections", "cpu_percent", "memory_percent", "recent_avg_latency_ms")

# Heartbeats arrive every second; one older than this means the agent (or its
# backend) has stopped, so its CPU/memory no longer describe the backend.
MAX_HEARTBEAT_AGE_S = 3.0


def feature_row(backend: Backend, stats: "BackendStats", now: float) -> list[float]:
    """The model's input for one backend, or NoPrediction if a feature is unknown."""
    if stats.heartbeat_at is None or now - stats.heartbeat_at > MAX_HEARTBEAT_AGE_S:
        raise NoPrediction(f"no fresh heartbeat from {backend}")
    if stats.avg_latency_ms is None:
        raise NoPrediction(f"no latency history for {backend} yet")
    return [float(stats.active_connections), stats.cpu_percent, stats.memory_percent, stats.avg_latency_ms]


def load_model(path: Path) -> Any:
    """Load a model saved by ml/train.py and check it expects our features."""
    import joblib  # ships with scikit-learn; imported here so `import lb` stays light
    saved = joblib.load(path)
    if tuple(saved.get("features", ())) != FEATURES:
        raise ValueError(f"model was trained on features {saved.get('features')}, expected {FEATURES}")
    return saved


class MLLatencyStrategy(GuardedStrategy):
    name = "ml_latency"

    def __init__(self, model_path: Path | str | None = None, time_budget_ms: float = 2.0,
                 model: Any = None) -> None:
        """Pass `model_path` (a file from ml/train.py) or, in tests, any object
        with a scikit-learn style predict(X) as `model`."""
        super().__init__(time_budget_ms)
        self.model = model
        self.model_info: dict = {}
        if model is None and model_path is not None:
            try:
                saved = load_model(Path(model_path))
                self.model = saved["model"]
                self.model_info = {k: v for k, v in saved.items() if k != "model"}
                log.info("ml_latency: loaded %s (%s, test MAE %.1f ms)", model_path,
                         saved.get("name"), saved.get("test_mae_ms", float("nan")))
            except Exception as exc:  # missing file, unreadable pickle, wrong features
                # Start anyway: every request falls back to least connections (and says so).
                log.warning("ml_latency: cannot load model %s (%s); using least connections", model_path, exc)

    @classmethod
    def from_config(cls, cfg: "LBConfig") -> "MLLatencyStrategy":
        from lb.config import PROJECT_ROOT  # local import: lb.config imports this package
        path = Path(cfg.ml_latency.model_path)
        return cls(path if path.is_absolute() else PROJECT_ROOT / path, cfg.ml_latency.time_budget_ms)

    def _choose(self, backends: Sequence[Backend], request: "HTTPRequest") -> Backend:
        if self.model is None:
            raise NoPrediction("no model loaded")
        if self._stats is None:
            raise RuntimeError("ml_latency needs live stats: create it through a Router")
        snapshot = self._stats.snapshot()
        now = time.monotonic()
        rows = []
        for backend in backends:
            if backend not in snapshot:
                raise NoPrediction(f"no stats for {backend} yet")
            rows.append(feature_row(backend, snapshot[backend], now))
        # One predict() call for all backends: most of its cost is fixed
        # per call (input checks), not per row.
        predicted = self.model.predict(np.array(rows, dtype=float))
        if len(predicted) != len(backends) or not np.isfinite(predicted).all():
            raise NoPrediction(f"model returned {predicted!r}")
        best = min(predicted)
        tied = [b for b, p in zip(backends, predicted) if p == best]
        return tied[0] if len(tied) == 1 else self.fallback.choose(tied, request)
