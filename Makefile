# Common commands. `make help` lists them. Every target is a thin wrapper
# around a command that is also documented in README.md, so nothing here is
# required: it only saves typing.
#
# The Docker targets (up, demo, down, logs) need only Docker. The rest run
# Python on this machine and need `make setup` first.

# Used only to create .venv; must be 3.11+ (the LB uses tomllib).
PYTHON ?= python3
PY ?= .venv/bin/python
DEMO_RATE ?= 60

.DEFAULT_GOAL := help
.PHONY: help setup up demo down logs init-db test test-unit lb dashboard traffic \
        baselines train experiments experiment quick-experiment plots static overhead clean

help:  ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-18s %s\n", $$1, $$2}'

# --- the system, in Docker -------------------------------------------------------------

up:  ## start everything (Postgres, LB, 3 backends, dashboard) and wait until healthy -> http://127.0.0.1:5050
	docker compose up -d --build --wait
	@echo "dashboard: http://127.0.0.1:5050   load balancer: http://127.0.0.1:8080"

demo: up  ## `make up`, then start background load (DEMO_RATE=60 req/s) so the dashboard moves
	curl -fsS --retry 10 --retry-connrefused --retry-delay 1 -X POST http://127.0.0.1:5050/api/control/load \
	     -H 'Content-Type: application/json' \
	     -d '{"rate": $(DEMO_RATE)}' > /dev/null
	@echo "demo load running at $(DEMO_RATE) req/s: open http://127.0.0.1:5050"

down:  ## stop and remove the containers (the database stays in the pgdata volume)
	docker compose down

logs:  ## follow the load balancer's log (warnings: failovers, fallbacks, strategy switches)
	docker compose logs -f lb

# --- development on this machine ------------------------------------------------------

setup:  ## create .venv, install the pinned dependencies, create .env from the example
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "need Python 3.11+, found " + sys.version.split()[0] + "; run: make setup PYTHON=python3.12")'
	$(PYTHON) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

init-db:  ## create the tables, indexes and views (idempotent; the LB container also does this on start)
	$(PY) scripts/init_db.py

test:  ## run every test (needs `make up` for the database tests)
	$(PY) -m pytest

test-unit:  ## run only the tests that need no database
	$(PY) -m pytest -m "not db"

lb:  ## run the LB from source with lb.toml (first `docker compose stop lb`: same ports)
	$(PY) -m lb.server

dashboard:  ## run the dashboard from source (first `docker compose stop dashboard`: same port)
	$(PY) -m dashboard.app

traffic:  ## send 60 s of traffic at 50 req/s through the LB on port 8080
	$(PY) -m experiments.load_generator --port 8080 --rate 50 --duration 60

# --- experiments -------------------------------------------------------------------------

baselines:  ## round robin and least connections, 4 scenarios x 3 repeats (~13 min); training data for the model
	$(PY) -m experiments.run_experiments --strategies round_robin least_connections --seed 42 --repeats 3

train:  ## export training data from the newest baseline batch and retrain the model (one ships in ml/models/)
	$(PY) -m ml.export_data --batch latest
	$(PY) -m ml.train

experiments:  ## final comparison: 4 strategies x 4 scenarios x 5 repeats, seed 2000 (~42 min)
	$(PY) -m experiments.run_experiments

experiment: experiments  ## same as `make experiments`

quick-experiment:  ## a 12 s look at the traffic spike, all four strategies, one repeat (~1 min; results in experiments/results/partial/)
	$(PY) -m experiments.run_experiments --scenarios traffic_spike --repeats 1 --duration 12

plots:  ## redraw the charts for the latest full batch
	$(PY) -m experiments.plot_results

static:  ## export the latest full comparison batch to docs/ (the static results page for GitHub Pages)
	$(PY) -m experiments.export_static

overhead:  ## measure the LB's own overhead per request (~15 s)
	$(PY) -m experiments.benchmark_overhead

clean:  ## delete caches (keeps .venv, the model, the database and the results)
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
	rm -rf .pytest_cache
