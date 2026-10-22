# Common commands. `make help` lists them. Every target is a thin wrapper
# around a command that is also documented in README.md, so nothing here is
# required: it only saves typing.

# Used only to create .venv; must be 3.11+ (the LB uses tomllib).
PYTHON ?= python3
PY ?= .venv/bin/python

.DEFAULT_GOAL := help
.PHONY: help setup up init-db down test test-unit lb dashboard traffic \
        baselines train experiments quick-experiment plots clean

help:  ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-18s %s\n", $$1, $$2}'

setup:  ## create .venv, install the pinned dependencies, create .env from the example
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "need Python 3.11+, found " + sys.version.split()[0] + "; run: make setup PYTHON=python3.12")'
	$(PYTHON) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example (change the password)")

up:  ## start Postgres, the three backends and the dashboard; wait until healthy; apply the schema
	docker compose up -d --build --wait
	$(PY) scripts/init_db.py

init-db:  ## create the tables, indexes and views (idempotent)
	$(PY) scripts/init_db.py

down:  ## stop the containers (data is kept in the pgdata volume)
	docker compose down

test:  ## run every test (needs `make up` for the database tests)
	$(PY) -m pytest

test-unit:  ## run only the tests that need no database
	$(PY) -m pytest -m "not db"

lb:  ## run the load balancer with lb.toml (port 8080, admin API on 8081)
	$(PY) -m lb.server

dashboard:  ## run the dashboard locally instead of in Docker (http://127.0.0.1:5050)
	$(PY) -m dashboard.app

traffic:  ## send 60 s of traffic at 50 req/s through the LB on port 8080
	$(PY) -m experiments.load_generator --port 8080 --rate 50 --duration 60

baselines:  ## Phase 5: round robin and least connections, 4 scenarios x 3 repeats (~13 min); training data for the model
	$(PY) -m experiments.run_experiments --strategies round_robin least_connections --seed 42 --repeats 3

train:  ## export training data from the newest baseline batch and train the latency model
	$(PY) -m ml.export_data --batch latest
	$(PY) -m ml.train

experiments:  ## final comparison: 4 strategies x 4 scenarios x 5 repeats, seed 2000 (~42 min; needs `make train`)
	$(PY) -m experiments.run_experiments

quick-experiment:  ## a 12 s look at the traffic spike, all four strategies, one repeat (~1 min; results in experiments/results/partial/)
	$(PY) -m experiments.run_experiments --scenarios traffic_spike --repeats 1 --duration 12

plots:  ## redraw the charts for the latest batch
	$(PY) -m experiments.plot_results

clean:  ## delete caches (keeps .venv, the model, the database and the results)
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
	rm -rf .pytest_cache
