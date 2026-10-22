# Connection pooling vs a new backend connection per request

Measured 2026-10-22 02:35 UTC by `python -m experiments.benchmark_pooling`. Real measured numbers; nothing estimated.

## Setup

- Machine: Darwin 25.1.0 arm64, 8 logical CPUs, Python 3.14.3
- 3 backend processes (`backend/app.py`, 0 ms artificial delay), LB with 32 workers, all on loopback
- Modes: `no-pool` = `[pool] size = 0`; `pool-32` = `[pool] size = 32`. Retries and rate limiting off in both.
- Closed-loop clients, 3.0 s per run, 3 run(s) per cell, mean shown. Clients open a new connection to the LB per request in BOTH modes; only LB -> backend differs.
- Backend connections per 1000 requests: from the LB's pool counters (admin API), i.e. how many TCP handshakes the LB made to backends
- TIME_WAIT: sockets on the backend ports in TIME_WAIT right after each run

## Raw cost of one TCP connection on loopback

`socket.create_connection()` to a backend, 2000 times: average 99.4 µs, median 30.8 µs for socket + 3-way handshake (close excluded). On loopback the round trip is almost free; across a real network each handshake adds one full RTT (often 0.5-50 ms), which is where pooling matters most.

## Results

| Clients | Mode | Throughput (req/s) | Avg latency (ms) | p95 latency (ms) | Backend conns / 1000 req | TIME_WAIT on backend ports | Errors |
|--------:|------|-------------------:|-----------------:|-----------------:|-------------------------:|---------------------------:|-------:|
| 1 | no-pool | 3695.6 | 0.3 | 0.3 | 1000.0 | 10775.7 | 0 |
| 1 | pool-32 | 4782.3 | 0.2 | 0.2 | 0.2 | 49.0 | 0 |
| 8 | no-pool | 5513.9 | 1.5 | 2.1 | 1000.0 | 14836.7 | 0 |
| 8 | pool-32 | 7273.3 | 1.1 | 1.6 | 0.5 | 53.0 | 0 |
| 32 | no-pool | 5379.0 | 5.9 | 7.1 | 1000.0 | 14485.7 | 0 |
| 32 | pool-32 | 7261.0 | 4.4 | 5.1 | 1.4 | 70.3 | 0 |

Per-run numbers are in `pooling_benchmark.json`.
