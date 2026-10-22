"""Train latency-prediction models (linear regression, decision tree / random forest).

    python -m ml.export_data --batch baseline-20261022T030347Z   # first: the CSV
    python -m ml.train

Supervised regression in plain words: every row is an example "a backend in
this state (active connections, CPU, memory, recent average latency) took
this long (response_time_ms)". A regression model is a function fitted to
those examples so that, given a new state, it outputs a number: the expected
response time. "Supervised" because every example comes with the right
answer to learn from.

Models compared, all on the same rows:
  - baseline: no learning at all, predict the backend's recent average
    latency (the EWMA feature itself). A model that cannot beat this is not
    worth running per request.
  - linear regression: latency = w1*active + w2*cpu + w3*mem + w4*avg + b.
    Fast and explainable, but only straight-line effects.
  - decision tree: a learned flowchart of yes/no questions ("active > 7?"),
    which can express thresholds such as "a backend with all 8 slots busy
    starts queueing". Depth-limited, because a tree that keeps splitting can
    memorise its training rows (overfitting).
  - deep tree: the same with no limits, kept in the report to SHOW
    overfitting: near-perfect on training rows, worse on new ones.
  - random forest: the average of many trees, each trained on a random
    resample, which smooths out the mistakes of single trees.

Error metric: MAE (mean absolute error) = average of |predicted - actual| in
ms. "On average the prediction is X ms off", in the target's own unit, and
one huge outlier does not dominate it the way it would a squared error.

Train/test split BY TIME, not at random: within every run the first 70% of
the run's seconds are training data and the last 30% test data. Consecutive
requests to a backend are nearly identical (same queue, same average), so a
random split puts a near-twin of almost every test row into the training set
and the model is graded on what it has effectively already seen. Live, the
model always predicts the future from the past; a time split grades it the
same way. The report shows both, to make the difference visible.

Choosing the model: the lowest test MAE among those whose prediction fits
the strategy's time budget (MLLatencySettings.time_budget_ms), since a model
that is always too slow would always be replaced by least connections.
Choosing by the same test set that is reported makes that one number
slightly optimistic; with five fixed candidates and no tuning the effect is
small, and the real test is the routing experiment (new runs, Phase 6).

Output: ml/models/latency_model.joblib (git-ignored) and
experiments/results/ml_training.md (+ .json).
"""

import argparse
import csv
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
from sklearn.tree import DecisionTreeRegressor

from experiments.benchmark_concurrency import RESULTS_DIR, percentile
from lb.config import PROJECT_ROOT, MLLatencySettings
from lb.strategies.ml_latency import FEATURES
from ml.export_data import DATA_PATH

MODEL_PATH = PROJECT_ROOT / MLLatencySettings().model_path  # where the LB looks by default
TEST_FRACTION = 0.3  # last 30% of every run's timeline


def load_rows(path: Path = DATA_PATH) -> list[dict]:
    """Rows usable for training: every feature known, and a successful request
    (a 5xx's time says how fast it failed, not how fast the backend serves)."""
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if any(row[name] == "" for name in FEATURES) or int(row["status_code"]) >= 500:
                continue
            for key in (*FEATURES, "t_in_run_s", "response_time_ms"):
                row[key] = float(row[key])
            rows.append(row)
    return rows


def time_split(rows: list[dict], test_fraction: float = TEST_FRACTION) -> tuple[list[dict], list[dict]]:
    """Per run: rows in the first (1 - test_fraction) of its timeline train, the rest test."""
    run_end: dict[str, float] = {}
    for r in rows:
        run_end[r["run_id"]] = max(run_end.get(r["run_id"], 0.0), r["t_in_run_s"])
    train, test = [], []
    for r in rows:
        (test if r["t_in_run_s"] >= (1 - test_fraction) * run_end[r["run_id"]] else train).append(r)
    return train, test


def random_split(rows: list[dict], test_fraction: float, seed: int) -> tuple[list[dict], list[dict]]:
    """For comparison only: the split that leaks (see the module docstring)."""
    mask = np.random.default_rng(seed).random(len(rows)) < test_fraction
    return [r for r, m in zip(rows, mask) if not m], [r for r, m in zip(rows, mask) if m]


def matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    return (np.array([[r[name] for name in FEATURES] for r in rows], dtype=float),
            np.array([r["response_time_ms"] for r in rows], dtype=float))


def candidates(seed: int) -> dict:
    return {
        "linear_regression": LinearRegression(),
        "decision_tree": DecisionTreeRegressor(max_depth=8, min_samples_leaf=20, random_state=seed),
        "deep_tree": DecisionTreeRegressor(random_state=seed),  # no limits: the overfitting demo
        "random_forest": RandomForestRegressor(n_estimators=30, max_depth=10, min_samples_leaf=10,
                                               random_state=seed, n_jobs=1),
    }


def mae(predicted: np.ndarray, actual: np.ndarray) -> float:
    return float(np.mean(np.abs(predicted - actual)))


def inference_us(model, X: np.ndarray, calls: int = 2000) -> dict:
    """Time one predict() on 3 rows (one per backend), as the strategy calls it."""
    rows = X[:3]
    model.predict(rows)  # warm-up
    times = []
    for _ in range(calls):
        start = time.perf_counter()
        model.predict(rows)
        times.append((time.perf_counter() - start) * 1e6)
    times.sort()
    return {"median_us": statistics.median(times), "p99_us": percentile(times, 99)}


def describe(model) -> dict:
    """What the model relies on: coefficients for the line, importances for trees."""
    if hasattr(model, "coef_"):
        return {"coefficients": dict(zip(FEATURES, map(float, model.coef_))), "intercept": float(model.intercept_)}
    return {"importances": dict(zip(FEATURES, map(float, model.feature_importances_)))}


def train(rows: list[dict], seed: int, budget_ms: float) -> dict:
    train_rows, test_rows = time_split(rows)
    X_train, y_train = matrix(train_rows)
    X_test, y_test = matrix(test_rows)
    avg_index = FEATURES.index("recent_avg_latency_ms")
    scenarios = sorted({r["scenario"] for r in rows})
    test_scenario = np.array([r["scenario"] for r in test_rows])

    def per_scenario(predicted: np.ndarray) -> dict:
        return {s: mae(predicted[test_scenario == s], y_test[test_scenario == s]) for s in scenarios}

    # Two regimes with very different errors: requests stuck behind round
    # robin's growing backlog (seconds) and everything else. The ML strategy
    # routes in the second one, and the first dominates the overall MAE.
    backlog, normal = y_test > 1000, y_test < 200

    def per_regime(predicted: np.ndarray) -> dict:
        return {"under_200ms": mae(predicted[normal], y_test[normal]),
                "over_1s": mae(predicted[backlog], y_test[backlog])}

    results = {"baseline_recent_avg": {
        "train_mae_ms": mae(X_train[:, avg_index], y_train), "test_mae_ms": mae(X_test[:, avg_index], y_test),
        "test_mae_by_scenario": per_scenario(X_test[:, avg_index]),
        "test_mae_by_regime": per_regime(X_test[:, avg_index])}}
    fitted = {}
    for name, model in candidates(seed).items():
        started = time.perf_counter()
        model.fit(X_train, y_train)
        fit_s = time.perf_counter() - started
        predicted = model.predict(X_test)
        fitted[name] = model
        results[name] = {"train_mae_ms": mae(model.predict(X_train), y_train), "test_mae_ms": mae(predicted, y_test),
                         "test_mae_by_scenario": per_scenario(predicted),
                         "test_mae_by_regime": per_regime(predicted), "fit_s": fit_s,
                         "inference": inference_us(model, X_test), **describe(model)}
    # Do CPU and memory help at all? The same line fitted without them
    # (informational only: the saved model always uses all of FEATURES).
    keep = [FEATURES.index("active_connections"), FEATURES.index("recent_avg_latency_ms")]
    ablation = LinearRegression().fit(X_train[:, keep], y_train)
    results_no_heartbeat = {"test_mae_ms": mae(ablation.predict(X_test[:, keep]), y_test),
                            "test_mae_by_scenario": per_scenario(ablation.predict(X_test[:, keep]))}
    # The leak, measured: the same forest graded on a random split.
    r_train, r_test = random_split(rows, TEST_FRACTION, seed)
    (Xr_train, yr_train), (Xr_test, yr_test) = matrix(r_train), matrix(r_test)
    leaky = candidates(seed)["random_forest"].fit(Xr_train, yr_train)
    random_split_mae = {"random_forest": mae(leaky.predict(Xr_test), yr_test),
                        "baseline_recent_avg": mae(Xr_test[:, avg_index], yr_test)}

    fits = [n for n in fitted if results[n]["inference"]["p99_us"] / 1000 <= budget_ms]
    if not fits:
        raise SystemExit(f"no model predicts within the {budget_ms} ms budget")
    best = min(fits, key=lambda n: results[n]["test_mae_ms"])
    return {"rows": len(rows), "train_rows": len(train_rows), "test_rows": len(test_rows),
            "test_rows_under_200ms": int(normal.sum()), "test_rows_over_1s": int(backlog.sum()),
            "train_max_ms": float(y_train.max()), "test_max_ms": float(y_test.max()),
            "runs": len({r["run_id"] for r in rows}), "scenarios": scenarios, "seed": seed,
            "budget_ms": budget_ms, "results": results, "random_split_mae_ms": random_split_mae,
            "linear_without_heartbeat_features": results_no_heartbeat,
            "best": best, "model": fitted[best]}


def write_report(summary: dict, source: str, path: Path) -> None:
    res = summary["results"]
    best = summary["best"]
    names = ["baseline_recent_avg", "linear_regression", "decision_tree", "deep_tree", "random_forest"]
    lines = [
        "# Latency model: training results", "",
        f"Data: {source}: {summary['rows']} successful requests with every feature known, from "
        f"{summary['runs']} runs. Time split per run: first {1 - TEST_FRACTION:.0%} of each run's seconds train "
        f"({summary['train_rows']} rows), last {TEST_FRACTION:.0%} test ({summary['test_rows']} rows). "
        f"Seed {summary['seed']}. Produced by `python -m ml.train`; measured, not estimated.", "",
        "| Model | Train MAE (ms) | Test MAE (ms) | predict() on 3 rows: median / p99 (µs) |",
        "|---|---:|---:|---:|",
    ]
    for name in names:
        r = res[name]
        timing = (f"{r['inference']['median_us']:.0f} / {r['inference']['p99_us']:.0f}"
                  if "inference" in r else "– (no model)")
        mark = " **(saved)**" if name == best else ""
        lines.append(f"| {name}{mark} | {r['train_mae_ms']:.1f} | {r['test_mae_ms']:.1f} | {timing} |")
    lines += ["", "Test MAE by scenario (ms):", "",
              "| Model | " + " | ".join(summary["scenarios"]) + " |",
              "|---|" + "---:|" * len(summary["scenarios"])]
    for name in names:
        by = res[name]["test_mae_by_scenario"]
        lines.append(f"| {name} | " + " | ".join(f"{by[s]:.1f}" for s in summary["scenarios"]) + " |")
    lines += ["", f"Test MAE by regime (ms). {summary['test_rows_over_1s']} test rows waited behind round robin's "
              f"growing backlog (over 1 s; the slowest took {summary['test_max_ms']:.0f} ms, while the slowest "
              f"training row took {summary['train_max_ms']:.0f} ms) and {summary['test_rows_under_200ms']} were "
              "under 200 ms, the regime the ML strategy actually routes in:", "",
              "| Model | Under 200 ms | Over 1 s |", "|---|---:|---:|"]
    for name in names:
        by = res[name]["test_mae_by_regime"]
        lines.append(f"| {name} | {by['under_200ms']:.1f} | {by['over_1s']:.1f} |")
    lines += ["", "The overall ranking is decided by the backlog rows: a tree can only predict values it saw in "
              "training, so it cannot follow a backlog that keeps growing past the training maximum, while the "
              "line extrapolates. Under 200 ms no model beats simply predicting the recent average. The saved "
              "model was chosen by the overall test MAE, the rule fixed before training; it was not re-chosen "
              "after seeing this.", ""]
    lr = res["linear_regression"]
    lines += ["What the models rely on:", "",
              "- Linear regression: " + ", ".join(f"{k} {v:+.3g}" for k, v in lr["coefficients"].items())
              + f", intercept {lr['intercept']:+.3g} (ms per unit of each feature).",
              "- Random forest feature importances: " + ", ".join(
                  f"{k} {v:.1%}" for k, v in res["random_forest"]["importances"].items()) + ".",
              "- Decision tree feature importances: " + ", ".join(
                  f"{k} {v:.1%}" for k, v in res["decision_tree"]["importances"].items()) + ".", "",
              f"Without the heartbeat features (CPU, memory), the same linear regression scores "
              f"{summary['linear_without_heartbeat_features']['test_mae_ms']:.1f} ms test MAE (vs "
              f"{lr['test_mae_ms']:.1f} ms with them): what they add, measured. In these experiments every "
              "backend is the same sleeping Python process, so their CPU and memory barely differ.", "",
              f"Random split instead of time split (the misleading way): random forest test MAE "
              f"{summary['random_split_mae_ms']['random_forest']:.1f} ms vs "
              f"{res['random_forest']['test_mae_ms']:.1f} ms with the time split; the recent-average baseline "
              f"{summary['random_split_mae_ms']['baseline_recent_avg']:.1f} ms vs "
              f"{res['baseline_recent_avg']['test_mae_ms']:.1f} ms.", "",
              f"Saved: `{best}`, the lowest test MAE among models whose p99 predict() time fits the "
              f"{summary['budget_ms']:g} ms per-request budget.", ""]
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=DATA_PATH)
    parser.add_argument("--out", type=Path, default=MODEL_PATH)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    rows = load_rows(args.data)
    if not rows:
        raise SystemExit(f"no usable rows in {args.data}; run python -m ml.export_data first")
    budget_ms = MLLatencySettings().time_budget_ms
    summary = train(rows, args.seed, budget_ms)
    batches = sorted({r.get("batch", "") for r in rows} - {""})
    source = f"`{args.data.relative_to(PROJECT_ROOT) if args.data.is_relative_to(PROJECT_ROOT) else args.data}`"
    best = summary["best"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": summary["model"], "name": best, "features": list(FEATURES),
                 "test_mae_ms": summary["results"][best]["test_mae_ms"],
                 "baseline_test_mae_ms": summary["results"]["baseline_recent_avg"]["test_mae_ms"],
                 "trained_on": {"rows": summary["rows"], "runs": summary["runs"], "batches": batches,
                                "scenarios": summary["scenarios"]},
                 "sklearn_version": sklearn.__version__,
                 "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}, args.out)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    report = {k: v for k, v in summary.items() if k != "model"}
    (RESULTS_DIR / "ml_training.json").write_text(json.dumps(report, indent=2) + "\n")
    write_report(summary, source, RESULTS_DIR / "ml_training.md")
    for name, r in summary["results"].items():
        print(f"{name:<20} train MAE {r['train_mae_ms']:8.1f} ms   test MAE {r['test_mae_ms']:8.1f} ms")
    print(f"saved {best} to {args.out} and the report to {RESULTS_DIR / 'ml_training.md'}")


if __name__ == "__main__":
    main()
