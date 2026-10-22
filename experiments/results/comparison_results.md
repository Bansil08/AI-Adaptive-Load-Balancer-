# Strategy comparison: round robin, least connections, ML latency, bandit

Batch `comparison-20261022T155233Z` (seed 42, 3 repeats per cell), produced by `python -m experiments.run_experiments`. Real measured numbers; nothing estimated.

Latency is the load balancer's `response_time_ms` over successful requests; error rate is 5xx over all requests; throughput is successful requests divided by the time taken to serve them (load start to last response, at least the planned duration). Each cell is the mean over the repeats, p95 with its min–max range. Retries off, health checks every 1 s, circuit breaker on, capacity 8 requests at a time per backend. Fallbacks: share of an ML strategy's requests routed by least connections instead (no fresh heartbeat or latency history yet, an error, or over the 2 ms decision budget).

## unequal_servers

Three backends of different speeds (20 / 60 / 150 ms), steady 90 req/s. Round robin gives the slow one a third of the traffic (30 req/s, ~59% of its 51 req/s capacity).

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 85.3 | 168.0 [168–168] | 0.00% | 90.5 | fast 33%, medium 33%, slow 33% | – |
| least_connections | 57.9 | 164.8 [165–165] | 0.00% | 90.5 | fast 57%, medium 28%, slow 15% | – |
| ml_latency | 27.3 | 29.9 [30–30] | 0.00% | 90.8 | fast 99%, medium 1%, slow 1% | 1.56% |
| bandit | 32.7 | 68.8 [69–69] | 0.00% | 90.7 | fast 93%, medium 4%, slow 4% | 0.00% |

## sudden_slowdown

Three equal 30 ms backends at 90 req/s; at t = 15 s backend b becomes ten times slower (300 ms). Its capacity drops to ~25 req/s, below the 30 req/s round robin keeps sending it.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 336.1 | 2387.5 [1980–2698] | 0.00% | 81.3 | a 33%, b 33%, c 33% | – |
| least_connections | 49.5 | 50.1 [45–57] | 0.00% | 90.2 | a 40%, b 21%, c 39% | – |
| ml_latency | 38.7 | 41.4 [40–43] | 0.00% | 90.6 | a 41%, b 18%, c 41% | 1.42% |
| bandit | 43.8 | 41.3 [40–42] | 0.00% | 90.3 | a 47%, b 15%, c 38% | 0.00% |

## traffic_spike

Unequal backends (20 / 60 / 150 ms); the rate jumps from 30 to 180 req/s at t = 10 s. A third of 180 (60 req/s) exceeds the slow backend's ~51 req/s capacity.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 728.2 | 3637.6 [3355–3818] | 0.00% | 113.6 | fast 33%, medium 33%, slow 33% | – |
| least_connections | 56.1 | 164.5 [164–165] | 0.00% | 129.4 | fast 58%, medium 28%, slow 14% | – |
| ml_latency | 27.1 | 32.5 [32–33] | 0.00% | 129.7 | fast 99%, medium 0%, slow 0% | 0.67% |
| bandit | 31.9 | 67.6 [67–68] | 0.00% | 129.6 | fast 93%, medium 3%, slow 3% | 0.00% |

## server_crash

Three equal 30 ms backends at 90 req/s; backend b is killed (SIGKILL) at t = 15 s and not restarted.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 36.0 | 38.8 [39–39] | 0.18% | 90.6 | a 42%, b 17%, c 42% | – |
| least_connections | 36.1 | 39.0 [39–39] | 0.18% | 90.6 | a 42%, b 17%, c 42% | – |
| ml_latency | 36.8 | 40.0 [40–40] | 0.20% | 90.6 | a 41%, b 17%, c 41% | 1.21% |
| bandit | 36.1 | 39.3 [39–40] | 0.15% | 90.6 | a 27%, b 19%, c 55% | 0.02% |

## Cross-check

Every run's request log was compared with the load generator: 48 of 48 runs logged exactly as many requests as the client received answers for.

Per-run numbers (and client-side latencies) are in `comparison_results.json`; raw per-request records in `runs/run_<id>.csv` (not committed).
