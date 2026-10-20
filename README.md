# adaptive-lb: AI-Adaptive Load Balancer

An HTTP load balancer built on raw sockets and threads in Python. It routes traffic across backends of different speeds using round robin, least connections and two ML-based strategies, performs health checks and failover, logs every request to PostgreSQL, and includes an experiment harness that compares the strategies under different traffic scenarios.

> **Status:** Phase 2 (thread pool, shared stats, graceful shutdown). See `PROJECT_SPEC.md` for the full plan and `CONCEPTS.md` for the feature → CS concept map.

## Prerequisites

- Python 3.11+
- Docker Desktop (with Docker Compose v2)

## Setup

```bash
# 1. Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Database credentials (edit the password in .env)
cp .env.example .env

# 3. Start PostgreSQL 16 and wait until it is healthy
docker compose up -d
docker compose ps          # STATUS should say "(healthy)"
# Postgres is exposed on host port 5433 (set in .env) to avoid clashing
# with a locally installed Postgres on the default 5432.

# 4. Create the tables
python scripts/init_db.py  # creates missing tables; safe to re-run
# After editing schema.sql, use --reset (drops all data): IF NOT EXISTS
# never alters a table that already exists.

# 5. Run the tests
pytest
```

To stop the database: `docker compose down` (data is kept in the `pgdata` volume; `docker compose down -v` deletes it).

## Run the load balancer

```bash
# Postgres + three test backends: fast (20 ms) :9001, medium (60 ms) :9002, slow (150 ms) :9003
docker compose up -d

# The LB reads backends, port, strategy and timeouts from lb.toml
python -m lb.server

# In another terminal: round robin rotates fast -> medium -> slow
for i in 1 2 3 4 5 6; do curl -s localhost:8080/; done
```

Stop a backend (`docker compose stop backend-slow`) and its turns return `502 Bad Gateway`. A backend slower than `backend_read` in `lb.toml` returns `504 Gateway Timeout`. Health checks that skip dead backends arrive in Phase 3.

Connections are served by a fixed pool of worker threads fed by a bounded queue; `[concurrency]` in `lb.toml` sets `workers`, `queue_size` and `shutdown_timeout`. Ctrl+C (SIGINT) or `kill` (SIGTERM) shuts down gracefully: new connections are refused, in-flight requests finish (for up to `shutdown_timeout` seconds), then the process exits. A second Ctrl+C exits immediately. `--thread-per-connection` switches to the Phase 1 design for comparison.

### Benchmark: thread pool vs thread-per-connection

```bash
python -m experiments.benchmark_concurrency   # ~25 min (waits for TIME_WAIT between runs); starts its own backends and LBs
```

Results (with the exact setup) are saved to `experiments/results/concurrency_benchmark.md` and `.json`.

Without Docker, start the backends as processes instead (one terminal each):

```bash
python -m backend.app --port 9001 --name fast   --delay-ms 20
python -m backend.app --port 9002 --name medium --delay-ms 60
python -m backend.app --port 9003 --name slow   --delay-ms 150
```

## Repository layout

| Path | Purpose |
|------|---------|
| `lb/` | The load balancer: listener + thread pool, HTTP parser, proxy, strategies, health checks, rate limiter, DB writer |
| `backend/` | Test backend servers with configurable delay, plus a UDP heartbeat agent |
| `db/` | `schema.sql` (3NF schema) and `queries.sql` |
| `ml/` | Training-data export, model training and evaluation |
| `experiments/` | Load generator, scenarios, experiment runner and plotting |
| `dashboard/` | Flask + Chart.js dashboard |
| `scripts/` | Helper scripts (`init_db.py`) |
| `lb.toml` | Load balancer config: backends, listen port, strategy, timeouts |
| `phase-*.md` | The instructions for each build phase |
| `tests/` | pytest suite |

## Architecture, results and analysis

Added in later phases.
