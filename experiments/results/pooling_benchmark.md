# Connection pooling vs a new backend connection per request

Measured 2026-10-22 01:14 UTC by `python -m experiments.benchmark_pooling`. Real measured numbers; nothing estimated.

## Setup

- Machine: Darwin 25.1.0 arm64, 8 logical CPUs, Python 3.14.3
- 3 backend processes (`backend/app.py`, 0 ms artificial delay), LB with 32 workers, all on loopback
- Modes: `no-pool` = `[pool] size = 0`; `pool-32` = `[pool] size = 32`. Retries and rate limiting off in both.
- Closed-loop clients, 3.0 s per run, 3 run(s) per cell, mean shown. Clients open a new connection to the LB per request in BOTH modes; only LB -> backend differs.
- Backend connections per 1000 requests: from the LB's pool counters (admin API), i.e. how many TCP handshakes the LB made to backends
- TIME_WAIT: sockets on the backend ports in TIME_WAIT right after each run

## Raw cost of one TCP connection on loopback

`socket.create_connection()` to a backend, 2000 times: average 99.5 µs, median 32.6 µs for socket + 3-way handshake (close excluded). On loopback the round trip is almost free; across a real network each handshake adds one full RTT (often 0.5-50 ms), which is where pooling matters most.

## Results

| Clients | Mode | Throughput (req/s) | Avg latency (ms) | p95 latency (ms) | Backend conns / 1000 req | TIME_WAIT on backend ports | Errors |
|--------:|------|-------------------:|-----------------:|-----------------:|-------------------------:|---------------------------:|-------:|
| 1 | no-pool | 3629.3 | 0.3 | 0.3 | 1000.0 | 10772.0 | 0 |
| 1 | pool-32 | 4880.2 | 0.2 | 0.2 | 0.2 | 49.7 | 0 |
| 8 | no-pool | 5494.8 | 1.4 | 2.0 | 1000.0 | 14779.0 | 0 |
| 8 | pool-32 | 6739.8 | 1.2 | 1.7 | 0.6 | 53.7 | 0 |
| 32 | no-pool | 5301.1 | 6.1 | 7.9 | 1000.0 | 14269.3 | 0 |
| 32 | pool-32 | 6799.9 | 4.7 | 5.8 | 1.5 | 69.7 | 0 |

Per-run numbers are in `pooling_benchmark.json`.
