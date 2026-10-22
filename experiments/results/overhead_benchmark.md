# Load balancer overhead per request

Measured 2026-10-22 23:01 UTC by `python -m experiments.benchmark_overhead`. Real measured numbers; nothing estimated.

Setup: Darwin 25.1.0 arm64, Python 3.14.3. One backend with a 0 ms delay; 3000 sequential GETs per row, each on a new client connection; LB with 32 workers and connection pooling, rate limiting and the database off. Overhead = through-LB minus direct.

| Path | Median (ms) | p95 (ms) | Mean (ms) | Median overhead (ms) |
|---|---:|---:|---:|---:|
| Direct to the backend | 0.155 | 0.176 | 0.163 | – |
| Through the LB, round_robin | 0.202 | 0.263 | 0.210 | +0.046 |
| Through the LB, least_connections | 0.202 | 0.223 | 0.207 | +0.047 |
| Through the LB, ml_latency | 0.254 | 0.288 | 0.259 | +0.098 |
| Through the LB, bandit | 0.204 | 0.231 | 0.208 | +0.048 |

Why the extra hop costs so little: both paths open one new TCP connection per request (the client's), but behind the LB the backend connection is pooled and reused, while the direct client makes the backend accept a new connection every time. The overhead is therefore the LB's parsing, routing and copying, minus the backend's connection setup it saves. The ML strategy adds its model call (~34 µs, `ml_inference.md`) on top. On loopback; across a real network the extra hop also adds a round trip.
