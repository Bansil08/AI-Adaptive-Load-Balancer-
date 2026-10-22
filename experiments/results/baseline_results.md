# Baseline experiments: round robin vs least connections

Batch `baseline-20261022T030347Z` (seed 42, 3 repeats per cell), produced by `python -m experiments.run_experiments`. Real measured numbers; nothing estimated.

Latency is the load balancer's `response_time_ms` over successful requests; error rate is 5xx over all requests; throughput is successful requests divided by the time taken to serve them (load start to last response, at least the planned duration). Each cell is the mean over the repeats, p95 with its min–max range. Retries off, health checks every 1 s, circuit breaker on, capacity 8 requests at a time per backend.

## unequal_servers

Three backends of different speeds (20 / 60 / 150 ms), steady 90 req/s. Round robin gives the slow one a third of the traffic (30 req/s, ~59% of its 51 req/s capacity).

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests |
|---|---:|---:|---:|---:|---|
| round_robin | 87.2 | 170.4 [169–172] | 0.00% | 90.4 | fast 33%, medium 33%, slow 33% |
| least_connections | 59.9 | 166.3 [166–166] | 0.00% | 90.6 | fast 56%, medium 29%, slow 15% |

## sudden_slowdown

Three equal 30 ms backends at 90 req/s; at t = 15 s backend b becomes ten times slower (300 ms). Its capacity drops to ~25 req/s, below the 30 req/s round robin keeps sending it.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests |
|---|---:|---:|---:|---:|---|
| round_robin | 342.8 | 2437.5 [2026–2744] | 0.00% | 81.1 | a 33%, b 33%, c 33% |
| least_connections | 50.3 | 44.8 [44–46] | 0.00% | 90.1 | a 40%, b 21%, c 39% |

## traffic_spike

Unequal backends (20 / 60 / 150 ms); the rate jumps from 30 to 180 req/s at t = 10 s. A third of 180 (60 req/s) exceeds the slow backend's ~51 req/s capacity.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests |
|---|---:|---:|---:|---:|---|
| round_robin | 721.3 | 3596.1 [3299–3761] | 0.00% | 113.8 | fast 33%, medium 33%, slow 33% |
| least_connections | 55.5 | 164.1 [164–164] | 0.00% | 129.4 | fast 59%, medium 28%, slow 14% |

## server_crash

Three equal 30 ms backends at 90 req/s; backend b is killed (SIGKILL) at t = 15 s and not restarted.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests |
|---|---:|---:|---:|---:|---|
| round_robin | 35.8 | 38.7 [39–39] | 0.18% | 90.6 | a 42%, b 17%, c 42% |
| least_connections | 35.8 | 38.8 [39–39] | 0.20% | 90.6 | a 42%, b 17%, c 42% |

## Cross-check

Every run's request log was compared with the load generator: 24 of 24 runs logged exactly as many requests as the client received answers for.

Per-run numbers (and client-side latencies) are in `baseline_results.json`; raw per-request records in `runs/run_<id>.csv` (not committed).
