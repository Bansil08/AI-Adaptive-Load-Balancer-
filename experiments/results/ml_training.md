# Latency model: training results

Data: `ml/data/training_data.csv`: 70823 successful requests with every feature known, from 24 runs. Time split per run: first 70% of each run's seconds train (46060 rows), last 30% test (24763 rows). Seed 42. Produced by `python -m ml.train`; measured, not estimated.

| Model | Train MAE (ms) | Test MAE (ms) | predict() on 3 rows: median / p99 (µs) |
|---|---:|---:|---:|
| baseline_recent_avg | 15.8 | 60.7 | – (no model) |
| linear_regression **(saved)** | 9.8 | 22.6 | 27 / 32 |
| decision_tree | 5.7 | 77.7 | 31 / 42 |
| deep_tree | 0.0 | 80.5 | 30 / 34 |
| random_forest | 5.0 | 80.5 | 684 / 745 |

Test MAE by scenario (ms):

| Model | server_crash | sudden_slowdown | traffic_spike | unequal_servers |
|---|---:|---:|---:|---:|
| baseline_recent_avg | 1.8 | 85.8 | 107.4 | 3.6 |
| linear_regression | 3.3 | 44.1 | 29.8 | 6.6 |
| decision_tree | 2.1 | 129.1 | 128.3 | 3.7 |
| deep_tree | 2.8 | 130.0 | 133.7 | 5.3 |
| random_forest | 3.0 | 137.6 | 130.6 | 3.6 |

Test MAE by regime (ms). 2452 test rows waited behind round robin's growing backlog (over 1 s; the slowest took 7557 ms, while the slowest training row took 3456 ms) and 22079 were under 200 ms, the regime the ML strategy actually routes in:

| Model | Under 200 ms | Over 1 s |
|---|---:|---:|
| baseline_recent_avg | 3.3 | 582.3 |
| linear_regression | 6.1 | 172.4 |
| decision_tree | 3.5 | 752.2 |
| deep_tree | 4.6 | 770.7 |
| random_forest | 3.6 | 779.5 |

The overall ranking is decided by the backlog rows: a tree can only predict values it saw in training, so it cannot follow a backlog that keeps growing past the training maximum, while the line extrapolates. Under 200 ms no model beats simply predicting the recent average. The saved model was chosen by the overall test MAE, the rule fixed before training; it was not re-chosen after seeing this.

What the models rely on:

- Linear regression: active_connections +3.09, cpu_percent -3.65, memory_percent -12.4, recent_avg_latency_ms +1.04, intercept -0.572 (ms per unit of each feature).
- Random forest feature importances: active_connections 5.0%, cpu_percent 0.1%, memory_percent 0.1%, recent_avg_latency_ms 94.9%.
- Decision tree feature importances: active_connections 6.6%, cpu_percent 0.0%, memory_percent 0.0%, recent_avg_latency_ms 93.4%.

Without the heartbeat features (CPU, memory), the same linear regression scores 22.5 ms test MAE (vs 22.6 ms with them): what they add, measured. In these experiments every backend is the same sleeping Python process, so their CPU and memory barely differ.

Random split instead of time split (the misleading way): random forest test MAE 6.8 ms vs 80.5 ms with the time split; the recent-average baseline 32.6 ms vs 60.7 ms.

Saved: `linear_regression`, the lowest test MAE among models whose p99 predict() time fits the 2 ms per-request budget.
