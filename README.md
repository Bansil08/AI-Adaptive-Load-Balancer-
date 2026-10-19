# adaptive-lb: AI-Adaptive Load Balancer

An HTTP load balancer built on raw sockets and threads in Python. It routes traffic across backends of different speeds using round robin, least connections and two ML-based strategies, performs health checks and failover, logs every request to PostgreSQL, and includes an experiment harness that compares the strategies under different traffic scenarios.

> **Status:** Phase 0 (project skeleton). See `PROJECT_SPEC.md` for the full plan and `CONCEPTS.md` for the feature → CS concept map.

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
python scripts/init_db.py  # add --reset to drop and recreate them

# 5. Run the tests
pytest
```

To stop the database: `docker compose down` (data is kept in the `pgdata` volume; `docker compose down -v` deletes it).

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
| `tests/` | pytest suite |

## Architecture, results and analysis

Added in later phases.
