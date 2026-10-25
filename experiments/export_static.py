"""Export an experiment batch for the static results page in docs/ (GitHub Pages).

    python -m experiments.export_static                       # the latest full comparison batch
    python -m experiments.export_static --batch comparison-20261022T212032Z
    python -m experiments.export_static --video-url https://youtu.be/...

Writes:
  docs/data/results.json   the batch's cells (mean, SD, min, max per scenario and
                           strategy), headline and winner cards (for p50, p95 and
                           p99), and methodology:
                           everything the page shows, so it needs no server;
  docs/assets/             results.js and dashboard.css copied from dashboard/static/,
                           so the static page draws exactly what the live dashboard does.

The numbers come from the database through the same code the dashboard uses
(dashboard.app.batch_cells, dashboard.summary), never typed in by hand.
"""

import argparse
import json
import shutil
from datetime import datetime, timezone

import psycopg

from dashboard.app import batch_cells
from dashboard.summary import headline, scenario_cards, summaries
from experiments.plot_results import latest_batch
from experiments.scenarios import SCENARIOS
from lb.config import PROJECT_ROOT, database_url

DOCS = PROJECT_ROOT / "docs"
REPO_URL = "https://github.com/Bansil08/AI-Adaptive-Load-Balancer-"

METHODOLOGY_SQL = """
SELECT count(*) AS runs,
       min((notes::jsonb ->> 'seed')::int) AS first_seed,
       max((notes::jsonb ->> 'seed')::int) AS last_seed,
       max((notes::jsonb ->> 'repeat')::int) + 1 AS repeats,
       min((notes::jsonb ->> 'duration')::float8) AS duration_s,
       sum((notes::jsonb ->> 'logged_rows')::int) AS requests,
       count(*) FILTER (WHERE (notes::jsonb ->> 'logged_rows') = (notes::jsonb ->> 'client_answered')) AS cross_checked
FROM experiment_runs
WHERE notes::jsonb ->> 'batch' = %s
"""

# Requests per run, per scenario: how many samples each run's percentiles rest on.
REQUESTS_PER_RUN_SQL = """
SELECT scenario, round(avg((notes::jsonb ->> 'logged_rows')::int)) AS requests
FROM experiment_runs
WHERE notes::jsonb ->> 'batch' = %s
GROUP BY scenario
"""


def export(batch: str | None = None, video_url: str | None = None) -> dict:
    with psycopg.connect(database_url()) as conn:
        # A comparison batch (all four strategies): a newer full baseline batch
        # (`make baselines`, round robin + least connections only) is not the result to publish.
        batch = batch or latest_batch(conn, prefix="comparison-")
        runs, first_seed, last_seed, repeats, duration, requests, cross_checked = conn.execute(
            METHODOLOGY_SQL, (batch,)).fetchone()
        # n is NULL for a scenario whose runs never finished (no logged_rows in their notes).
        per_run = {scenario: int(n) for scenario, n in conn.execute(REQUESTS_PER_RUN_SQL, (batch,)).fetchall()
                   if n is not None}
    if not runs:
        raise SystemExit(f"no runs found for batch {batch!r}")
    cells = batch_cells(batch)
    data = {
        "batch": batch,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cells": cells,
        "headline": headline(cells),
        "cards": scenario_cards(cells),
        "by_percentile": summaries(cells),
        "methodology": {
            "runs": runs, "repeats": repeats, "seeds": [first_seed, last_seed], "duration_s": duration,
            "requests": requests, "cross_checked": cross_checked, "requests_per_run": per_run,
            "strategies": len({c["strategy"] for c in cells}), "scenarios": len({c["scenario"] for c in cells}),
            "scenario_descriptions": {name: s.description for name, s in SCENARIOS.items()
                                      if any(c["scenario"] == name for c in cells)},
        },
        "links": {"repo": REPO_URL, "video": video_url},
    }
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    (DOCS / "data" / "results.json").write_text(json.dumps(data, indent=1) + "\n")
    (DOCS / "assets").mkdir(parents=True, exist_ok=True)
    for name in ("results.js", "dashboard.css"):
        shutil.copyfile(PROJECT_ROOT / "dashboard" / "static" / name, DOCS / "assets" / name)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch", help="experiment batch label (default: the latest full comparison batch)")
    parser.add_argument("--video-url", help="link to the demo video, shown on the page")
    args = parser.parse_args()
    data = export(args.batch, args.video_url)
    print(f"exported {data['batch']} ({data['methodology']['runs']} runs) to {DOCS / 'data' / 'results.json'}")
    if data["headline"]:
        print(f"headline: {data['headline']['text']}")


if __name__ == "__main__":
    main()
