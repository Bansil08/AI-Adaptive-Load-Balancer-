# Latency model: evaluation and per-request overhead

Model: `linear_regression` trained 2026-10-22T15:47:20+00:00 on 72066 requests (baseline-20261022T030347Z). Produced by `python -m ml.evaluate`; measured on this machine, not estimated.

## MAE on later runs (`comparison_baselines.csv`)

72080 successful requests the model never saw. Model MAE 14.7 ms vs 31.3 ms for the "predict the recent average" baseline.

| Scenario | Model MAE (ms) | Baseline MAE (ms) |
|---|---:|---:|
| server_crash | 3.1 | 1.8 |
| sudden_slowdown | 20.8 | 35.3 |
| traffic_spike | 24.7 | 68.8 |
| unequal_servers | 5.9 | 2.6 |

## Time per routing decision

`strategy.choose()` for 3 backends, timed around each call. With 32 threads all choosing at once, a call also waits for the GIL while other threads run.

| Strategy | Threads | Decisions | Median (µs) | p99 (µs) | Max (µs) | Over 2 ms |
|---|---:|---:|---:|---:|---:|---:|
| least_connections | 1 | 5000 | 4 | 10 | 103 | 0.00% |
| least_connections | 32 | 16000 | 4 | 10 | 379 | 0.00% |
| ml_latency | 1 | 5000 | 35 | 86 | 581 | 0.00% |
| ml_latency | 32 | 16000 | 34 | 59 | 466484 | 0.56% |
