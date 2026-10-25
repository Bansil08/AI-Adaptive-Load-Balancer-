# Strategy comparison: round robin, least connections, ML latency, bandit

Batch `comparison-20261022T212032Z` (seed 2000, 5 repeats per cell), produced by `python -m experiments.run_experiments`. Real measured numbers; nothing estimated.

Latency is the load balancer's `response_time_ms` over successful requests; error rate is 5xx over all requests; throughput is successful requests divided by the time taken to serve them (load start to last response, at least the planned duration). Each cell is the mean ± sample standard deviation over the repeats, with the percentiles' min–max range in brackets. Percentiles are computed per run from all successful requests; p99 rests on the slowest 1% of them, so it is noisier than p95. Retries off, health checks every 1 s, circuit breaker on, capacity 8 requests at a time per backend. Fallbacks: share of an ML strategy's requests routed by least connections instead (no fresh heartbeat or latency history yet, an error, or over the 2 ms decision budget).

## unequal_servers

Three backends of different speeds (20 / 60 / 150 ms), steady 90 req/s. Round robin gives the slow one a third of the traffic (30 req/s, ~59% of its 51 req/s capacity).

| Strategy | p50 (ms) [min–max] | p95 (ms) [min–max] | p99 (ms) [min–max] | Avg latency (ms) | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| round_robin | 69.3 ± 1.7 [67–71] | 170.0 ± 2.2 [167–172] | 173.3 ± 2.5 [170–175] | 86.8 ± 1.5 | 0.00% | 90.2 ± 1.3 | fast 33%, medium 33%, slow 33% | – |
| least_connections | 30.1 ± 2.6 [27–32] | 166.3 ± 1.9 [164–168] | 171.6 ± 2.2 [169–174] | 59.8 ± 2.0 | 0.00% | 90.3 ± 1.2 | fast 56%, medium 29%, slow 15% | – |
| ml_latency | 27.8 ± 1.7 [26–29] | 31.3 ± 2.6 [28–34] | 52.1 ± 14.7 [38–69] | 28.2 ± 1.4 | 0.00% | 90.6 ± 1.3 | fast 99%, medium 1%, slow 0% | 1.57% |
| bandit | 27.7 ± 1.7 [26–29] | 70.4 ± 2.0 [68–73] | 167.9 ± 2.3 [164–170] | 33.8 ± 1.6 | 0.00% | 90.5 ± 1.3 | fast 93%, medium 3%, slow 4% | 0.00% |

## sudden_slowdown

Three equal 30 ms backends at 90 req/s; at t = 15 s backend b becomes ten times slower (300 ms). Its capacity drops to ~25 req/s, below the 30 req/s round robin keeps sending it.

| Strategy | p50 (ms) [min–max] | p95 (ms) [min–max] | p99 (ms) [min–max] | Avg latency (ms) | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| round_robin | 38.0 ± 1.6 [37–40] | 2333.1 ± 175.1 [2038–2483] | 3120.7 ± 181.1 [2874–3314] | 321.8 ± 20.3 | 0.00% | 81.7 ± 1.2 | a 33%, b 33%, c 33% | – |
| least_connections | 37.1 ± 1.2 [36–39] | 43.6 ± 2.5 [40–46] | 327.6 ± 1.1 [327–329] | 49.3 ± 1.2 | 0.00% | 90.3 ± 1.2 | a 40%, b 21%, c 39% | – |
| ml_latency | 37.7 ± 1.2 [37–39] | 41.9 ± 2.1 [40–45] | 46.7 ± 1.4 [45–49] | 38.6 ± 1.2 | 0.00% | 90.5 ± 1.3 | a 41%, b 17%, c 42% | 0.68% |
| bandit | 36.9 ± 1.1 [36–39] | 41.5 ± 1.6 [40–44] | 320.2 ± 5.4 [314–327] | 43.3 ± 2.4 | 0.00% | 90.4 ± 1.4 | a 36%, b 23%, c 41% | 0.01% |

## traffic_spike

Unequal backends (20 / 60 / 150 ms); the rate jumps from 30 to 180 req/s at t = 10 s. A third of 180 (60 req/s) exceeds the slow backend's ~51 req/s capacity.

| Strategy | p50 (ms) [min–max] | p95 (ms) [min–max] | p99 (ms) [min–max] | Avg latency (ms) | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| round_robin | 67.3 ± 0.2 [67–68] | 3839.8 ± 337.7 [3344–4105] | 4336.3 ± 273.2 [3975–4596] | 748.2 ± 65.5 | 0.00% | 113.8 ± 0.6 | fast 33%, medium 33%, slow 33% | – |
| least_connections | 27.3 ± 0.1 [27–27] | 164.0 ± 0.3 [164–164] | 169.0 ± 0.3 [169–169] | 55.7 ± 0.3 | 0.00% | 130.0 ± 1.2 | fast 59%, medium 28%, slow 14% | – |
| ml_latency | 26.1 ± 0.1 [26–26] | 33.2 ± 2.5 [30–37] | 51.6 ± 15.0 [37–70] | 26.9 ± 0.6 | 0.00% | 130.4 ± 1.2 | fast 99%, medium 0%, slow 0% | 0.63% |
| bandit | 25.9 ± 0.1 [26–26] | 68.0 ± 1.0 [67–69] | 165.2 ± 0.3 [165–166] | 32.0 ± 0.6 | 0.00% | 130.3 ± 1.3 | fast 93%, medium 3%, slow 4% | 0.00% |

## server_crash

Three equal 30 ms backends at 90 req/s; backend b is killed (SIGKILL) at t = 15 s and not restarted.

| Strategy | p50 (ms) [min–max] | p95 (ms) [min–max] | p99 (ms) [min–max] | Avg latency (ms) | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---:|---:|---|---:|
| round_robin | 36.1 ± 0.1 [36–36] | 38.9 ± 0.2 [39–39] | 41.0 ± 1.1 [40–42] | 36.0 ± 0.1 | 0.19% | 90.3 ± 1.2 | a 42%, b 17%, c 42% | – |
| least_connections | 36.2 ± 0.1 [36–36] | 38.8 ± 0.1 [39–39] | 40.6 ± 0.9 [40–42] | 36.0 ± 0.1 | 0.18% | 90.4 ± 1.3 | a 42%, b 17%, c 42% | – |
| ml_latency | 36.7 ± 0.1 [37–37] | 39.6 ± 0.3 [39–40] | 42.7 ± 1.9 [40–44] | 36.6 ± 0.2 | 0.18% | 90.3 ± 1.3 | a 42%, b 17%, c 42% | 0.60% |
| bandit | 36.1 ± 0.1 [36–36] | 38.9 ± 0.2 [39–39] | 42.1 ± 2.4 [40–45] | 36.0 ± 0.1 | 0.10% | 90.4 ± 1.3 | a 34%, b 21%, c 45% | 0.00% |

## Cross-check

Every run's request log was compared with the load generator: 80 of 80 runs logged exactly as many requests as the client received answers for.

Per-run numbers (and client-side latencies) are in `comparison_results.json`; raw per-request records in `runs/run_<id>.csv` (not committed).
