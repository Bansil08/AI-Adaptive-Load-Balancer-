# Thread pool vs thread-per-connection

Measured 2026-10-20 22:37 UTC by `python -m experiments.benchmark_concurrency`. Real measured numbers; nothing estimated.

## Setup

- Machine: Darwin 25.1.0 arm64, 8 logical CPUs, Python 3.14.3
- 3 backend processes, 50 ms artificial delay each (I/O-bound: they sleep)
- Thread pool sizes: 32, 256 workers, queue size 128; listen backlog 128
- Closed-loop clients (new TCP connection per request), 5.0 s per run, 3 run(s) per cell; table shows the mean over runs (peak threads: max)
- Peak threads = OS thread count of the LB process, sampled every 50 ms
- Before each run the script waits until fewer than 1000 sockets are in TIME_WAIT, so a run doesn't inherit the previous run's ephemeral-port usage

## Results

| Clients | Mode | Throughput (req/s) | Avg latency (ms) | p95 latency (ms) | Peak LB threads | Errors |
|--------:|------|-------------------:|-----------------:|-----------------:|----------------:|-------:|
| 10 | pool-32 | 173.5 | 57.5 | 61.8 | 33 | 0 |
| 10 | pool-256 | 172.9 | 57.7 | 62.9 | 257 | 0 |
| 10 | thread-per-connection | 172.2 | 58.0 | 63.0 | 11 | 0 |
| 50 | pool-32 | 549.5 | 90.2 | 115.5 | 33 | 0 |
| 50 | pool-256 | 869.9 | 57.3 | 63.0 | 257 | 0 |
| 50 | thread-per-connection | 872.2 | 57.0 | 62.3 | 51 | 0 |
| 200 | pool-32 | 564.1 | 342.0 | 376.6 | 33 | 0 |
| 200 | pool-256 | 3425.9 | 58.0 | 65.3 | 257 | 0 |
| 200 | thread-per-connection | 3378.1 | 58.8 | 67.9 | 201 | 0 |
| 500 | pool-32 | 439.7 | 823.8 | 4784.0 | 33 | 32 |
| 500 | pool-256 | 4275.8 | 115.3 | 144.7 | 257 | 0 |
| 500 | thread-per-connection | 4652.1 | 100.9 | 166.1 | 437 | 0 |

Per-run numbers are in `concurrency_benchmark.json`.
