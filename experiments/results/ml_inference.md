# Latency model: evaluation and per-request overhead

Model: `linear_regression` trained 2026-10-22T16:28:06+00:00 on 70823 requests (baseline-20261022T030347Z). Produced by `python -m ml.evaluate`; measured on this machine, not estimated.

## MAE on later runs (`comparison_baselines.csv`)

70070 successful requests the model never saw. Model MAE 13.6 ms vs 30.6 ms for the "predict the recent average" baseline.

| Scenario | Model MAE (ms) | Baseline MAE (ms) |
|---|---:|---:|
| server_crash | 3.0 | 1.8 |
| sudden_slowdown | 17.1 | 29.0 |
| traffic_spike | 23.7 | 70.0 |
| unequal_servers | 5.7 | 2.5 |

## Time per routing decision

`strategy.choose()` for 3 backends, timed around each call. With 32 threads all choosing at once, a call also waits for the GIL while other threads run.

| Strategy | Threads | Decisions | Median (µs) | p99 (µs) | Max (µs) | Over 2 ms |
|---|---:|---:|---:|---:|---:|---:|
| least_connections | 1 | 5000 | 4 | 4 | 18 | 0.00% |
| least_connections | 32 | 16000 | 4 | 4 | 27 | 0.00% |
| ml_latency | 1 | 5000 | 34 | 40 | 110 | 0.00% |
| ml_latency | 32 | 16000 | 34 | 48 | 472949 | 0.52% |
