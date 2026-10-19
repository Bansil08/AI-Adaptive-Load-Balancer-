# Project: AI-Adaptive Load Balancer (adaptive-lb)

You are helping me, a 2nd-year B.Tech student, build a resume project from scratch. The goal is for this project to demonstrate core CS fundamentals (Operating Systems, Computer Networks, DBMS, System Design) plus a small, honest ML component. I must be able to explain every line in interviews, so clarity and learning matter more than cleverness.

## What we are building

An HTTP load balancer written on raw sockets that sits in front of several backend servers of different speeds. It supports multiple routing strategies (round robin, least connections, and two ML-based strategies), performs health checks and failover, logs every request to PostgreSQL, and includes an experiment harness that compares all strategies under different traffic scenarios. A small web dashboard shows live traffic and latency.

The headline result I want at the end: a measured comparison like "the adaptive strategy reduced p95 latency by X% vs round robin when a server degraded", reported honestly even where ML does not win.

## Tech stack and hard constraints

- **Language:** Python 3.11+ for everything.
- **Proxy core:** use the `socket` and `threading` modules directly. Do NOT use aiohttp, FastAPI, nginx, HAProxy, or any HTTP proxy library for the load balancer itself. Parsing HTTP and forwarding bytes by hand is the point.
- **Concurrency:** threads with a fixed-size thread pool and a bounded queue (no asyncio in the load balancer, because I want to demonstrate producer-consumer, locks and condition variables).
- **Database:** PostgreSQL via Docker, accessed with `psycopg` (plain SQL, no ORM).
- **Backends:** simple Python HTTP servers (the standard library `http.server` is fine) run as separate processes/containers, with configurable artificial delay. Use Docker CPU limits to make some backends slower.
- **ML:** scikit-learn for the supervised model; the bandit is implemented by hand.
- **Dashboard:** a small Flask app with Chart.js, reading from PostgreSQL.
- **Orchestration:** Docker Compose for Postgres, backends and the dashboard.
- **Tests:** pytest.
- Keep everything at the level of a standard undergraduate OS/CN/DBMS syllabus. No epoll, no kernel bypass, no consensus algorithms.

## Repository structure (suggested; adjust if you have a good reason, and tell me why)

```
adaptive-lb/
  lb/
    server.py            # listener + bounded queue + thread pool
    http_parser.py       # request/response parsing by hand
    proxy.py             # forwarding logic, connection pooling, timeouts, retries
    router.py            # chooses backend via a strategy
    strategies/
      base.py            # LoadBalancingStrategy interface
      round_robin.py
      least_connections.py
      ml_latency.py      # supervised latency prediction
      bandit.py          # epsilon-greedy multi-armed bandit
    health.py            # TCP + HTTP health checks, circuit breaker
    heartbeat.py         # UDP listener for backend metrics
    stats.py             # shared server stats (readers-writers lock)
    rate_limiter.py      # token bucket per client IP
    db_writer.py         # single writer thread, batched inserts
    admin.py             # add/remove backends at runtime
    config.py
  backend/
    app.py               # test backend with configurable delay + /health
    agent.py             # sends CPU/memory/active-connection heartbeats over UDP
  db/
    schema.sql
    queries.sql          # dashboard + training-data queries
  ml/
    export_data.py
    train.py
    evaluate.py
  experiments/
    load_generator.py    # multithreaded load generator
    scenarios.py         # unequal servers, sudden slowdown, traffic spike, crash
    run_experiments.py
    plot_results.py
  dashboard/
  tests/
  docker-compose.yml
  README.md
  CONCEPTS.md            # maps every feature to the CS concept it demonstrates
```

## Database schema (PostgreSQL, 3NF)

- `servers` (id, address, port, capacity_label, status, created_at)
- `health_checks` (id, server_id FK, checked_at, check_type 'tcp'|'http', passed, latency_ms)
- `server_metrics` (id, server_id FK, recorded_at, cpu_percent, memory_percent, active_connections)
- `experiment_runs` (id, strategy, scenario, started_at, ended_at, notes)
- `requests` (id, run_id FK nullable, server_id FK, received_at, method, path, status_code, response_time_ms, retried)
- `run_results` (run_id FK, avg_latency_ms, p95_latency_ms, error_rate, throughput_rps)

Add an index on `requests(server_id, received_at)` and on `server_metrics(server_id, recorded_at)`, and show `EXPLAIN ANALYZE` before/after in CONCEPTS.md. Include a retention job that deletes rows older than 7 days.

## Build phases

Work strictly one phase at a time. At the end of each phase: run the tests, summarise what was built, list the CS concepts it demonstrates, update CONCEPTS.md, and STOP so I can review and ask questions before you continue.

**Phase 0 — Setup.** Repo skeleton, docker-compose with Postgres, schema.sql applied, a README stub, pytest working.

**Phase 1 — Basic proxy (CN).** Backend test servers with configurable delay and a `/health` endpoint. Load balancer accepts TCP connections, parses HTTP/1.1 requests by hand (request line, headers, Content-Length body), forwards to backends using round robin, adds `X-Forwarded-For`, returns the response. Tests for the parser.

**Phase 2 — Concurrency (OS).** Listener thread puts accepted connections into a bounded queue; a fixed thread pool consumes them (producer-consumer with a condition variable or semaphore). Shared server stats protected with a readers-writers lock. Fixed lock ordering documented. Graceful shutdown on SIGINT/SIGTERM that drains in-flight requests. Measure thread-pool vs thread-per-connection and record the result.

**Phase 3 — Reliability (CN + system design).** TCP-level and HTTP-level health checks in a background thread. UDP heartbeats from a backend agent carrying CPU, memory and active connections. Circuit breaker per backend (closed / open / half-open). Connection pooling to backends with a measured comparison against opening a new connection each time. Timeouts, and retries only for idempotent methods (GET/HEAD) with exponential backoff. Token-bucket rate limiting per client IP. Optional sticky sessions by hashing client IP. Admin endpoint to add/remove backends at runtime.

**Phase 4 — Persistence (DBMS).** Workers push log records into an in-memory queue; a single DB writer thread inserts them in batched transactions. Indexes and EXPLAIN ANALYZE comparison. Dashboard queries: p95 latency per server per minute, error rate, requests per server. Retention job.

**Phase 5 — Experiments and baselines (system design).** Multithreaded load generator with configurable request rate. Four scenarios: (1) unequal servers, (2) one server slows down halfway, (3) traffic spike from low to high rate, (4) one server crashes mid-run. Least connections strategy. Run round robin and least connections on all scenarios, store results in `run_results`, and plot them.

**Phase 6 — ML strategies.** (a) Supervised: export training data via SQL, features = active connections, CPU %, memory %, recent average latency; target = response time; train linear regression and a decision tree/random forest; report MAE on a held-out set; route to the backend with the lowest predicted latency. (b) Bandit: epsilon-greedy over backends using negative latency as reward, with a sliding window or decay so it adapts to change. Both strategies must fall back to least connections if prediction fails or exceeds a small time budget. Keep inference cheap enough to run per request and measure its overhead.

**Phase 7 — Final evaluation and polish.** Run all four strategies across all four scenarios (multiple runs each), produce comparison graphs, finish the Flask + Chart.js dashboard, and write the README: architecture diagram, how to run, results with honest analysis (including where ML does not help and why), a section on the load balancer as a single point of failure and how a standby could take over, and future work.

## Working rules for you

1. One phase at a time; stop after each phase for my review.
2. Prefer simple, readable code over clever code. Add short comments explaining the *why* behind concurrency and networking decisions.
3. Before writing a non-trivial component, briefly explain the design and the CS concept behind it in 3–6 sentences.
4. Write tests for parsing, the strategies, the circuit breaker, the rate limiter and the DB writer.
5. Never fake or estimate benchmark numbers; only report what was actually measured, with the setup used.
6. Keep CONCEPTS.md up to date: a table of feature → file → CS concept (OS / CN / DBMS / system design / ML) → one-line explanation I can use in interviews.
7. If a requirement here seems wrong or there's a clearly better approach within the constraints, tell me before changing course.
8. Make a git commit at the end of each phase with a clear message.

Start with Phase 0.
