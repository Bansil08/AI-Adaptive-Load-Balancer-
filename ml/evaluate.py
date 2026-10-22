"""Evaluate the saved latency model: MAE on new data, and its per-request cost.

    python -m ml.evaluate                                  # overhead benchmark only
    python -m ml.evaluate --data ml/data/new_runs.csv      # + MAE on a CSV from ml.export_data

1. MAE on data the model has never seen. train.py's test set is the last
   30% of the SAME runs it trained on; the stronger check is runs recorded
   later (e.g. the round robin / least connections runs of the Phase 6
   comparison batch), exported with ml.export_data --batch <label>.

2. Inference overhead: how long one routing decision takes, timed around
   strategy.choose() for 3 backends, for least connections and for the ML
   strategy (feature lookup + predict() + the fallback checks), first on one
   thread and then with 32 threads choosing at once, as the LB's workers do.
   Python runs one thread's bytecode at a time (the GIL), so under
   contention a decision also waits for other threads; that waiting is
   real overhead and can push a decision past the time budget.

Output: experiments/results/ml_inference.md (+ .json).
"""

import argparse
import json
import statistics
import threading
import time
from pathlib import Path

import numpy as np

from experiments.benchmark_concurrency import RESULTS_DIR, percentile
from lb.http_parser import HTTPRequest
from lb.stats import ServerStats
from lb.strategies.base import Backend
from lb.strategies.least_connections import LeastConnectionsStrategy
from lb.strategies.ml_latency import FEATURES, MLLatencyStrategy, load_model
from ml.train import MODEL_PATH, load_rows, mae, matrix

BACKENDS = [Backend("127.0.0.1", 19001 + i) for i in range(3)]


def evaluate_csv(model, path: Path) -> dict:
    rows = load_rows(path)
    X, y = matrix(rows)
    avg = X[:, FEATURES.index("recent_avg_latency_ms")]
    scenarios = np.array([r["scenario"] for r in rows])
    predicted = model.predict(X)
    by = {s: {"model": mae(predicted[scenarios == s], y[scenarios == s]),
              "baseline": mae(avg[scenarios == s], y[scenarios == s])} for s in sorted(set(scenarios))}
    return {"rows": len(rows), "model_mae_ms": mae(predicted, y), "baseline_mae_ms": mae(avg, y), "by_scenario": by}


def live_stats() -> ServerStats:
    stats = ServerStats(BACKENDS)
    for i, b in enumerate(BACKENDS):
        stats.record_heartbeat(b, 0.3, 0.25, 0, time.monotonic() + 3600)  # stays "fresh" for the benchmark
        stats.request_started(b)
        stats.request_finished(b, 20.0 * (i + 1))
    return stats


def time_choices(strategy, threads: int, per_thread: int) -> list[float]:
    request = HTTPRequest("GET", "/", "HTTP/1.1", [], b"")
    start_together = threading.Barrier(threads)
    samples: list[list[float]] = [[] for _ in range(threads)]

    def run(out: list[float]) -> None:
        start_together.wait()
        for _ in range(per_thread):
            started = time.perf_counter()
            strategy.choose(BACKENDS, request)
            out.append((time.perf_counter() - started) * 1e6)

    workers = [threading.Thread(target=run, args=(s,)) for s in samples]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return sorted(t for s in samples for t in s)


def overhead(model_path: Path) -> dict:
    results = {}
    for label, make in (("least_connections", LeastConnectionsStrategy),
                        ("ml_latency", lambda: MLLatencyStrategy(model_path, time_budget_ms=1e9))):
        for threads, per_thread in ((1, 5000), (32, 500)):
            strategy = make()
            strategy.attach(live_stats())
            strategy.choose(BACKENDS, HTTPRequest("GET", "/", "HTTP/1.1", [], b""))  # warm-up
            times = time_choices(strategy, threads, per_thread)
            results[f"{label}/{threads}"] = {
                "strategy": label, "threads": threads, "decisions": len(times),
                "median_us": statistics.median(times), "p99_us": percentile(times, 99), "max_us": times[-1],
                "over_2ms": sum(t > 2000 for t in times) / len(times)}
    return results


def write_report(info: dict, timing: dict, held_out: dict | None, path: Path) -> None:
    lines = ["# Latency model: evaluation and per-request overhead", "",
             f"Model: `{info.get('name')}` trained {info.get('trained_at')} on {info['trained_on']['rows']} "
             f"requests ({', '.join(info['trained_on']['batches']) or 'unknown batch'}). Produced by "
             "`python -m ml.evaluate`; measured on this machine, not estimated.", ""]
    if held_out:
        lines += [f"## MAE on later runs ({held_out['source']})", "",
                  f"{held_out['rows']} successful requests the model never saw. Model MAE "
                  f"{held_out['model_mae_ms']:.1f} ms vs {held_out['baseline_mae_ms']:.1f} ms for the "
                  "\"predict the recent average\" baseline.", "",
                  "| Scenario | Model MAE (ms) | Baseline MAE (ms) |", "|---|---:|---:|"]
        lines += [f"| {s} | {v['model']:.1f} | {v['baseline']:.1f} |" for s, v in held_out["by_scenario"].items()]
        lines.append("")
    lines += ["## Time per routing decision", "",
              "`strategy.choose()` for 3 backends, timed around each call. With 32 threads all choosing at "
              "once, a call also waits for the GIL while other threads run.", "",
              "| Strategy | Threads | Decisions | Median (µs) | p99 (µs) | Max (µs) | Over 2 ms |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for r in timing.values():
        lines.append(f"| {r['strategy']} | {r['threads']} | {r['decisions']} | {r['median_us']:.0f} | "
                     f"{r['p99_us']:.0f} | {r['max_us']:.0f} | {r['over_2ms']:.2%} |")
    lines.append("")
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", type=Path, default=MODEL_PATH)
    parser.add_argument("--data", type=Path, help="CSV from ml.export_data with runs the model never saw")
    args = parser.parse_args()
    saved = load_model(args.model)
    info = {k: v for k, v in saved.items() if k != "model"}
    held_out = None
    if args.data:
        held_out = evaluate_csv(saved["model"], args.data) | {"source": f"`{args.data.name}`"}
        print(f"held-out MAE: model {held_out['model_mae_ms']:.1f} ms, baseline {held_out['baseline_mae_ms']:.1f} ms")
    timing = overhead(args.model)
    for key, r in timing.items():
        print(f"{key:<22} median {r['median_us']:7.1f} us   p99 {r['p99_us']:8.1f} us   over 2 ms {r['over_2ms']:.2%}")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "ml_inference.json").write_text(
        json.dumps({"model": info, "held_out": held_out, "timing": timing}, indent=2, default=str) + "\n")
    write_report(info, timing, held_out, RESULTS_DIR / "ml_inference.md")
    print(f"saved {RESULTS_DIR / 'ml_inference.md'}")


if __name__ == "__main__":
    main()
