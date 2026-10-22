# Latency model: training results

Data: `ml/data/training_data.csv`: 72066 successful requests with every feature known, from 24 runs. Time split per run: first 70% of each run's seconds train (47303 rows), last 30% test (24763 rows). Seed 42. Produced by `python -m ml.train`; measured, not estimated.

| Model | Train MAE (ms) | Test MAE (ms) | predict() on 3 rows: median / p99 (µs) |
|---|---:|---:|---:|
| baseline_recent_avg | 15.5 | 60.7 | – (no model) |
| linear_regression **(saved)** | 9.7 | 22.6 | 27 / 34 |
| decision_tree | 5.6 | 77.7 | 31 / 37 |
| deep_tree | 0.0 | 82.4 | 32 / 39 |
| random_forest | 4.9 | 80.5 | 691 / 754 |

Test MAE by scenario (ms):

| Model | server_crash | sudden_slowdown | traffic_spike | unequal_servers |
|---|---:|---:|---:|---:|
| baseline_recent_avg | 1.8 | 85.8 | 107.4 | 3.6 |
| linear_regression | 3.3 | 44.1 | 29.8 | 6.6 |
| decision_tree | 2.1 | 129.1 | 128.3 | 3.7 |
| deep_tree | 2.9 | 139.6 | 133.7 | 5.4 |
| random_forest | 2.9 | 141.6 | 128.5 | 3.6 |

What the models rely on:

- Linear regression: active_connections +3.09, cpu_percent -3.21, memory_percent -12, recent_avg_latency_ms +1.04, intercept -0.82 (ms per unit of each feature).
- Random forest feature importances: active_connections 5.9%, cpu_percent 0.1%, memory_percent 0.1%, recent_avg_latency_ms 94.0%.
- Decision tree feature importances: active_connections 6.6%, cpu_percent 0.0%, memory_percent 0.0%, recent_avg_latency_ms 93.4%.

Random split instead of time split (the misleading way): random forest test MAE 6.4 ms vs 80.5 ms with the time split; the recent-average baseline 31.9 ms vs 60.7 ms.

Saved: `linear_regression`, the lowest test MAE among models whose p99 predict() time fits the 2 ms per-request budget.
