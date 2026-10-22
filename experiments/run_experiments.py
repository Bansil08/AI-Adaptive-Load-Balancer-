"""Run every strategy on every scenario and store results in run_results.

One command, fixed seed:

    python -m experiments.run_experiments            # all 4 strategies x 4 scenarios, 3 repeats (~28 min)
    python -m experiments.run_experiments --scenarios traffic_spike --repeats 1 --duration 10

For each (strategy, scenario, repeat):
  1. insert an experiment_runs row; its id is the run_id;
  2. start fresh backends and a fresh load balancer (`--run-id`), so no
     stats, pooled connections or circuit-breaker state carry over between
     runs;
  3. send the scenario's load with the load generator, firing its events
     (slow a backend down, kill one) at their times;
  4. stop the LB (its DB writer flushes the request log), then compute avg
     and p95 latency, error rate and throughput IN SQL from the requests
     tagged with this run_id and insert them into run_results.

Fairness and repeatability: repeat i uses seed + i for the arrival times and
the backends' jitter, and every strategy gets the SAME seeds, so both see an
identical sequence of requests. OS scheduling still makes runs differ
slightly, which is why each cell is repeated and reported as mean and range.

Settings held constant for every run (see LB_CONFIG): retries off, so the
error rate shows what the routing strategy did rather than what retries hid;
rate limiting off (all load comes from one IP); 256 LB workers, so the LB's
own thread pool isn't what saturates. Health checks (every 1 s) and the
circuit breaker are on, as they would be in production.

Latency is the LB's response_time_ms (request fully received -> response
sent); the load generator's client-side numbers are recorded in the run's
notes as a cross-check.

ML strategies (Phase 6): ml_latency needs a trained model
(python -m ml.export_data, then python -m ml.train); every run counts how
often each ML strategy fell back to least connections (from the LB's log).

Results: experiments/results/<prefix>_results.md (+ .json) and the PNG charts
from experiments/plot_results.py, where <prefix> is "baseline" for a batch
of only round robin / least connections (Phase 5) and "comparison" once an
ML strategy is included (Phase 6), so the Phase 5 report is kept.
"""

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from experiments.benchmark_concurrency import RESULTS_DIR, free_port, wait_for_port
from experiments.load_generator import LoadGenerator, summarise
from experiments.scenarios import SCENARIOS, Scenario
from lb.config import PROJECT_ROOT, MLLatencySettings, database_url

MODEL_PATH = PROJECT_ROOT / MLLatencySettings().model_path  # where ml/train.py saves it

STRATEGIES = ("round_robin", "least_connections", "ml_latency", "bandit")
BASELINES = ("round_robin", "least_connections")
BACKEND_PORTS = (19001, 19002, 19003)  # fixed, so each backend keeps one row in `servers`
RUNS_DIR = RESULTS_DIR / "runs"        # raw client-side records per run (git-ignored)

LB_CONFIG = """\
strategy = "{strategy}"
backends = [{backends}]
[listen]
port = {port}
[timeouts]
client = 10.0
backend_connect = 1.0
backend_read = 30.0
[concurrency]
workers = 256
queue_size = 512
shutdown_timeout = 35.0
[health]
interval = 1.0
timeout = 0.5
unhealthy_threshold = 2
healthy_threshold = 2
[circuit_breaker]
failure_threshold = 5
cooldown = 5.0
half_open_trials = 2
[pool]
size = 16
[retries]
max_retries = 0
[rate_limit]
enabled = false
[heartbeat]
port = {heartbeat_port}
[database]
enabled = true
shutdown_timeout = 15.0
[bandit]
seed = {seed}
"""
# ml_latency uses the defaults: ml/models/latency_model.joblib, 2 ms budget.

FALLBACK_MARKER = "fallback to least connections"


def batch_prefix(strategies) -> str:
    """"baseline" for a Phase 5 batch, "comparison" once an ML strategy is in it."""
    return "baseline" if set(strategies) <= set(BASELINES) else "comparison"

RUN_RESULTS_SQL = """
INSERT INTO run_results (run_id, avg_latency_ms, p95_latency_ms, error_rate, throughput_rps)
SELECT %(run_id)s,
       -- Latency over successful requests only: a fast 502 would otherwise make
       -- a failing run look faster. Errors are counted separately.
       coalesce(avg(r.response_time_ms) FILTER (WHERE r.status_code < 500), 0),
       coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY r.response_time_ms)
                FILTER (WHERE r.status_code < 500), 0),
       coalesce(avg((r.status_code >= 500)::int), 0),
       -- Throughput = successful requests / the time it took to serve them: from
       -- the start of the load to the last response, or the planned duration if
       -- that is longer. Dividing by the planned 30 s alone credited a strategy
       -- whose backlog was still draining at 34 s with work done in overtime,
       -- which made every strategy look equal (a review caught this).
       count(*) FILTER (WHERE r.status_code < 500)
         / greatest(%(duration)s::float8,
                    extract(epoch FROM max(r.received_at + r.response_time_ms * interval '1 millisecond')
                                       - min(e.started_at)))
FROM requests r JOIN experiment_runs e ON e.id = r.run_id
WHERE r.run_id = %(run_id)s
RETURNING avg_latency_ms, p95_latency_ms, error_rate, throughput_rps
"""

SHARE_SQL = """
SELECT s.port, count(*)
FROM requests r JOIN servers s ON s.id = r.server_id
WHERE r.run_id = %s
GROUP BY s.port
"""


def start_backends(scenario: Scenario, heartbeat_port: int, seed: int) -> dict[str, tuple[subprocess.Popen, int]]:
    procs = {}
    for i, (spec, port) in enumerate(zip(scenario.backends, BACKEND_PORTS)):
        procs[spec.name] = (subprocess.Popen(
            [sys.executable, "-m", "backend.app", "--port", str(port), "--name", spec.name,
             "--delay-ms", str(spec.delay_ms), "--jitter-ms", str(spec.jitter_ms),
             "--capacity", str(spec.capacity), "--allow-control", "--seed", str(seed * 10 + i),
             "--heartbeat", f"127.0.0.1:{heartbeat_port}", "--quiet"],
            cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL), port)
    for _, port in procs.values():
        wait_for_port(port)
    return procs


def start_lb(strategy: str, scenario: Scenario, run_id: int, heartbeat_port: int, seed: int, tmp: Path):
    port = free_port()
    backends = ", ".join(f'"127.0.0.1:{p}"' for p in BACKEND_PORTS[:len(scenario.backends)])
    config = tmp / "lb.toml"
    config.write_text(LB_CONFIG.format(strategy=strategy, backends=backends, port=port,
                                       heartbeat_port=heartbeat_port, seed=seed))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    # To a file, not a pipe: nobody reads a pipe while the run is going, and
    # once its ~64 KB buffer filled (the crash scenario logs a warning per
    # failed request) the LB would block mid-log-call and freeze.
    with open(RUNS_DIR / f"run_{run_id}_lb.log", "w") as log_file:
        proc = subprocess.Popen([sys.executable, "-m", "lb.server", "--config", str(config), "--quiet",
                                 "--run-id", str(run_id)], cwd=PROJECT_ROOT, stderr=log_file)
    wait_for_port(port)
    return proc, port


def stop_lb(proc: subprocess.Popen) -> None:
    """SIGTERM and wait long enough for the drain AND the DB writer's flush.
    (A 10 s wait-then-kill could kill it mid-flush and silently lose rows.)"""
    proc.terminate()
    try:
        proc.wait(60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise RuntimeError("load balancer did not shut down within 60 s; its request log may be incomplete")


def set_delay(port: int, delay_ms: float) -> None:
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/control", body=json.dumps({"delay_ms": delay_ms, "jitter_ms": delay_ms * 0.1}))
        response = conn.getresponse()
        response.read()
        if response.status != 200:
            raise RuntimeError(f"control endpoint answered {response.status}")
    finally:
        conn.close()


def event_actions(scenario: Scenario, backends: dict[str, tuple[subprocess.Popen, int]]):
    actions = []
    for event in scenario.events:
        proc, port = backends[event.backend]
        if event.action == "kill":
            actions.append((event.at, lambda p=proc: p.kill()))           # SIGKILL: a crash
        elif event.action == "set_delay":
            actions.append((event.at, lambda port=port, ms=event.delay_ms: set_delay(port, ms)))
        else:
            raise ValueError(f"unknown event {event.action!r}")
    return actions


def run_once(conn: psycopg.Connection, strategy: str, scenario: Scenario, repeat: int, seed: int,
             batch: str, duration: float) -> dict:
    notes = {"batch": batch, "repeat": repeat, "seed": seed, "duration": duration,
             "backends": {spec.name: port for spec, port in zip(scenario.backends, BACKEND_PORTS)},
             "events": [vars(e) for e in scenario.events]}
    if strategy == "ml_latency":  # which model file routed this run, for traceability
        notes["model_sha256"] = hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()[:16]
    run_id = conn.execute("INSERT INTO experiment_runs (strategy, scenario, notes) VALUES (%s, %s, %s) RETURNING id",
                          (strategy, scenario.name, json.dumps(notes))).fetchone()[0]
    heartbeat_port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        backends = start_backends(scenario, heartbeat_port, seed)
        lb = None
        try:
            lb, lb_port = start_lb(strategy, scenario, run_id, heartbeat_port, seed, Path(tmp))
            results, load_started = LoadGenerator("127.0.0.1", lb_port).run(
                scenario.profile(), duration, seed, event_actions(scenario, backends))
        finally:
            if lb is not None:
                stop_lb(lb)  # SIGTERM: drains, then the DB writer flushes the request log
            for proc, _ in backends.values():
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    # started_at = the instant the load generator's clock started (the clock the
    # LB stamps requests with), so "seconds since start" lines up with the events.
    conn.execute("UPDATE experiment_runs SET started_at = to_timestamp(%s), ended_at = now() WHERE id = %s",
                 (load_started, run_id))
    avg, p95, error_rate, throughput = conn.execute(
        RUN_RESULTS_SQL, {"run_id": run_id, "duration": duration}).fetchone()
    logged = conn.execute("SELECT count(*) FROM requests WHERE run_id = %s", (run_id,)).fetchone()[0]
    port_to_name = {port: name for name, port in notes["backends"].items()}
    share = {port_to_name.get(port, str(port)): n for port, n in conn.execute(SHARE_SQL, (run_id,)).fetchall()}
    client = summarise(results, duration)
    # How often an ML strategy handed the decision to least connections (each is logged).
    log_text = (RUNS_DIR / f"run_{run_id}_lb.log").read_text(errors="replace")
    fallbacks = log_text.count(FALLBACK_MARKER)
    # Every request the client got an HTTP answer to should be in the LB's log.
    answered = sum(1 for r in results if r.status is not None)
    notes |= {"client": client, "logged_rows": logged, "client_answered": answered, "share": share,
              "fallbacks": fallbacks}
    conn.execute("UPDATE experiment_runs SET notes = %s WHERE id = %s", (json.dumps(notes), run_id))
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUNS_DIR / f"run_{run_id}.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["scheduled_s", "latency_ms", "status", "error", "backend"])
        writer.writerows((round(r.scheduled_s, 4), round(r.latency_ms, 3), r.status, r.error, r.backend)
                         for r in results)
    return {"run_id": run_id, "strategy": strategy, "scenario": scenario.name, "repeat": repeat,
            "avg_latency_ms": avg, "p95_latency_ms": p95, "error_rate": error_rate,
            "throughput_rps": throughput, "share": share, "logged_rows": logged,
            "client_answered": answered, "client": client, "fallbacks": fallbacks}


def recompute_batch(conn: psycopg.Connection, batch: str) -> list[dict]:
    """Rebuild run_results (and the per-run summaries) for a batch from the database, without re-running."""
    runs = []
    rows = conn.execute("SELECT id, strategy, scenario, notes FROM experiment_runs"
                        " WHERE notes::jsonb ->> 'batch' = %s ORDER BY id", (batch,)).fetchall()
    if not rows:
        raise SystemExit(f"no runs found for batch {batch!r}")
    for run_id, strategy, scenario, notes_text in rows:
        notes = json.loads(notes_text)
        if "share" not in notes:
            continue  # a run that never finished
        with conn.transaction():
            conn.execute("DELETE FROM run_results WHERE run_id = %s", (run_id,))
            avg, p95, error_rate, throughput = conn.execute(
                RUN_RESULTS_SQL, {"run_id": run_id, "duration": notes["duration"]}).fetchone()
        runs.append({"run_id": run_id, "strategy": strategy, "scenario": scenario, "repeat": notes["repeat"],
                     "avg_latency_ms": avg, "p95_latency_ms": p95, "error_rate": error_rate,
                     "throughput_rps": throughput, "share": notes["share"], "logged_rows": notes["logged_rows"],
                     "client_answered": notes["client_answered"], "client": notes["client"],
                     "fallbacks": notes.get("fallbacks", 0),
                     "base_seed": notes["seed"] - notes["repeat"]})  # repeat i ran with seed + i
    return runs


def aggregate(runs: list[dict]) -> dict:
    def stats(key):
        values = [r[key] for r in runs]
        return {"mean": statistics.fmean(values), "min": min(values), "max": max(values)}
    shares = {}
    for r in runs:
        for name, n in r["share"].items():
            shares[name] = shares.get(name, 0) + n
    total = sum(shares.values()) or 1
    return {"runs": len(runs), "avg_latency_ms": stats("avg_latency_ms"), "p95_latency_ms": stats("p95_latency_ms"),
            "error_rate": stats("error_rate"), "throughput_rps": stats("throughput_rps"),
            "share": {name: n / total for name, n in sorted(shares.items())},
            "fallback_rate": sum(r.get("fallbacks", 0) for r in runs) / max(1, sum(r["logged_rows"] for r in runs))}


def write_report(batch: str, seed: int, repeats: int, runs: list[dict], path: Path) -> None:
    scenarios = [name for name in SCENARIOS if any(r["scenario"] == name for r in runs)]
    strategies = [name for name in STRATEGIES if any(r["strategy"] == name for r in runs)]
    ml = any(name not in BASELINES for name in strategies)
    title = ("Strategy comparison: round robin, least connections, ML latency, bandit" if ml
             else "Baseline experiments: round robin vs least connections")
    lines = [
        f"# {title}", "",
        f"Batch `{batch}` (seed {seed}, {repeats} repeats per cell), produced by "
        "`python -m experiments.run_experiments`. Real measured numbers; nothing estimated.", "",
        "Latency is the load balancer's `response_time_ms` over successful requests; error rate is 5xx over "
        "all requests; throughput is successful requests divided by the time taken to serve them (load start "
        "to last response, at least the planned duration). Each cell is the mean over the repeats, p95 with "
        "its min–max range. Retries off, health checks every 1 s, circuit breaker on, capacity 8 requests at "
        "a time per backend."
        + (" Fallbacks: share of an ML strategy's requests routed by least connections instead (no fresh "
           "heartbeat or latency history yet, an error, or over the 2 ms decision budget)." if ml else ""), "",
    ]
    for name in scenarios:
        scenario = SCENARIOS[name]
        lines += [f"## {name}", "", scenario.description, "",
                  "| Strategy | Avg latency (ms) | p95 latency (ms) [min–max] | Error rate | Throughput (req/s) | Share of requests |"
                  + (" Fallbacks |" if ml else ""),
                  "|---|---:|---:|---:|---:|---|" + ("---:|" if ml else "")]
        for strategy in strategies:
            cell = [r for r in runs if r["scenario"] == name and r["strategy"] == strategy]
            if not cell:
                continue
            a = aggregate(cell)
            share = ", ".join(f"{k} {v:.0%}" for k, v in a["share"].items())
            lines.append(f"| {strategy} | {a['avg_latency_ms']['mean']:.1f} | {a['p95_latency_ms']['mean']:.1f} "
                         f"[{a['p95_latency_ms']['min']:.0f}–{a['p95_latency_ms']['max']:.0f}] | "
                         f"{a['error_rate']['mean']:.2%} | {a['throughput_rps']['mean']:.1f} | {share} |"
                         + ((f" {a['fallback_rate']:.2%} |" if strategy not in BASELINES else " – |") if ml else ""))
        lines.append("")
    mismatches = [r for r in runs if r["logged_rows"] != r["client_answered"]]
    lines += ["## Cross-check", "",
              f"Every run's request log was compared with the load generator: {len(runs) - len(mismatches)} of "
              f"{len(runs)} runs logged exactly as many requests as the client received answers for."
              + ("" if not mismatches else " Mismatches: " + ", ".join(
                  f"run {r['run_id']} ({r['logged_rows']} logged vs {r['client_answered']} answered)"
                  for r in mismatches)), "",
              f"Per-run numbers (and client-side latencies) are in `{path.stem}.json`; raw per-request "
              "records in `runs/run_<id>.csv` (not committed).", ""]
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategies", nargs="+", default=list(STRATEGIES), choices=STRATEGIES)
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--duration", type=float,
                        help="run every scenario for this many seconds (events and rate steps scale with it)")
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--recompute", metavar="BATCH",
                        help="rebuild run_results, the report and the charts for an existing batch; no new runs")
    args = parser.parse_args()

    if args.recompute:
        with psycopg.connect(database_url(), autocommit=True) as conn:
            runs = recompute_batch(conn, args.recompute)
        repeats = max(r["repeat"] for r in runs) + 1
        save(args.recompute, runs[0]["base_seed"], repeats, runs, plots=not args.no_plots)
        return

    if "ml_latency" in args.strategies and not MODEL_PATH.exists():
        parser.error(f"ml_latency needs {MODEL_PATH}: run python -m ml.export_data, then python -m ml.train")
    batch = datetime.now(timezone.utc).strftime(f"{batch_prefix(args.strategies)}-%Y%m%dT%H%M%SZ")
    runs = []
    with psycopg.connect(database_url(), autocommit=True) as conn:
        for name in args.scenarios:
            scenario = SCENARIOS[name].scaled(args.duration) if args.duration else SCENARIOS[name]
            duration = scenario.duration
            for repeat in range(args.repeats):
                for strategy in args.strategies:  # same seed for every strategy: identical arrivals
                    run = run_once(conn, strategy, scenario, repeat, args.seed + repeat, batch, duration)
                    runs.append(run)
                    print(f"{name:<16} {strategy:<18} repeat {repeat}: avg {run['avg_latency_ms']:7.1f} ms  "
                          f"p95 {run['p95_latency_ms']:7.1f} ms  errors {run['error_rate']:6.2%}  "
                          f"{run['throughput_rps']:6.1f} req/s  share {run['share']}  "
                          f"fallbacks {run['fallbacks']}", flush=True)
    save(batch, args.seed, args.repeats, runs, plots=not args.no_plots)


def save(batch: str, seed: int, repeats: int, runs: list[dict], plots: bool) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    prefix = batch_prefix({r["strategy"] for r in runs})
    (RESULTS_DIR / f"{prefix}_results.json").write_text(json.dumps(
        {"batch": batch, "seed": seed, "repeats": repeats, "runs": runs}, indent=2, default=str) + "\n")
    write_report(batch, seed, repeats, runs, RESULTS_DIR / f"{prefix}_results.md")
    print(f"saved {RESULTS_DIR / f'{prefix}_results.md'}")
    if plots:
        from experiments.plot_results import plot_batch
        for path in plot_batch(batch):
            print(f"saved {path}")


if __name__ == "__main__":
    main()
