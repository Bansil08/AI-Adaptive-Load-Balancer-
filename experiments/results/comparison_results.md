# Strategy comparison: round robin, least connections, ML latency, bandit

Batch `comparison-20261022T162847Z` (seed 1000, 3 repeats per cell), produced by `python -m experiments.run_experiments`. Real measured numbers; nothing estimated.

Latency is the load balancer's `response_time_ms` over successful requests; error rate is 5xx over all requests; throughput is successful requests divided by the time taken to serve them (load start to last response, at least the planned duration). Each cell is the mean over the repeats, p95 with its min–max range. Retries off, health checks every 1 s, circuit breaker on, capacity 8 requests at a time per backend. Fallbacks: share of an ML strategy's requests routed by least connections instead (no fresh heartbeat or latency history yet, an error, or over the 2 ms decision budget).

## unequal_servers

Three backends of different speeds (20 / 60 / 150 ms), steady 90 req/s. Round robin gives the slow one a third of the traffic (30 req/s, ~59% of its 51 req/s capacity).

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 85.2 | 167.5 [167–168] | 0.00% | 88.7 | fast 33%, medium 33%, slow 33% | – |
| least_connections | 58.2 | 164.8 [164–166] | 0.00% | 88.7 | fast 56%, medium 29%, slow 15% | – |
| ml_latency | 27.8 | 30.3 [29–32] | 0.00% | 89.0 | fast 98%, medium 1%, slow 1% | 3.07% |
| bandit | 31.8 | 67.7 [67–68] | 0.00% | 89.0 | fast 93%, medium 3%, slow 4% | 0.02% |

## sudden_slowdown

Three equal 30 ms backends at 90 req/s; at t = 15 s backend b becomes ten times slower (300 ms). Its capacity drops to ~25 req/s, below the 30 req/s round robin keeps sending it.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 293.1 | 2150.2 [1990–2308] | 0.00% | 81.0 | a 33%, b 33%, c 33% | – |
| least_connections | 48.9 | 43.1 [41–46] | 0.00% | 88.4 | a 40%, b 21%, c 40% | – |
| ml_latency | 37.4 | 39.7 [39–40] | 0.00% | 89.0 | a 41%, b 17%, c 42% | 0.56% |
| bandit | 43.7 | 40.0 [39–41] | 0.00% | 89.0 | a 27%, b 21%, c 52% | 0.00% |

## traffic_spike

Unequal backends (20 / 60 / 150 ms); the rate jumps from 30 to 180 req/s at t = 10 s. A third of 180 (60 req/s) exceeds the slow backend's ~51 req/s capacity.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 698.7 | 3600.8 [3436–3843] | 0.00% | 113.5 | fast 33%, medium 33%, slow 33% | – |
| least_connections | 55.4 | 164.0 [164–164] | 0.00% | 129.3 | fast 59%, medium 28%, slow 14% | – |
| ml_latency | 26.2 | 30.2 [29–31] | 0.00% | 129.7 | fast 100%, medium 0%, slow 0% | 0.28% |
| bandit | 31.4 | 67.5 [67–68] | 0.00% | 129.5 | fast 93%, medium 3%, slow 3% | 0.00% |

## server_crash

Three equal 30 ms backends at 90 req/s; backend b is killed (SIGKILL) at t = 15 s and not restarted.

| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests | Fallbacks |
|---|---:|---:|---:|---:|---|---:|
| round_robin | 36.0 | 38.9 [39–39] | 0.19% | 88.8 | a 42%, b 17%, c 42% | – |
| least_connections | 36.1 | 39.1 [39–39] | 0.19% | 88.8 | a 42%, b 17%, c 42% | – |
| ml_latency | 36.7 | 40.1 [40–40] | 0.20% | 88.8 | a 42%, b 17%, c 41% | 0.84% |
| bandit | 36.3 | 39.3 [39–40] | 0.20% | 88.8 | a 47%, b 23%, c 30% | 0.02% |

## Cross-check

Every run's request log was compared with the load generator: 48 of 48 runs logged exactly as many requests as the client received answers for.

Per-run numbers (and client-side latencies) are in `comparison_results.json`; raw per-request records in `runs/run_<id>.csv` (not committed).
