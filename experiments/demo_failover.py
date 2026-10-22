"""Failover demo: kill one backend under traffic, watch traffic move away, restart it, watch it rejoin.

Starts 3 backends (with UDP heartbeat agents) and the load balancer as
separate processes, then sends steady GET traffic from client threads. At
--kill-at seconds backend "b" is killed with SIGKILL (a crash: no clean
shutdown); at --restart-at it is started again on the same port. Every second
it prints how many requests each backend served, how many failed, and what
the admin API says about "b" (health and circuit state).

It runs twice by default: once with retries off, so the failures that happen
before the health checks and circuit breaker react are visible, and once with
the retries from lb.toml, which hide them from clients. Results are saved to
experiments/results/failover_demo.md.

Run:  python -m experiments.demo_failover
      python -m experiments.demo_failover --retries 2 --kill-at 3 --restart-at 9 --duration 16
"""

import argparse
import http.client
import json
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from experiments.benchmark_concurrency import PROJECT_ROOT, RESULTS_DIR, free_port, stop, wait_for_port

NAMES = ("a", "b", "c")
VICTIM = "b"


def start_backend(name: str, port: int, heartbeat_port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.app", "--port", str(port), "--name", name, "--delay-ms", "10",
         "--quiet", "--heartbeat", f"127.0.0.1:{heartbeat_port}"],
        cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL,
    )
    wait_for_port(port)
    return proc


def start_lb(ports: dict[str, int], retries: int, config_dir: Path) -> tuple[subprocess.Popen, int, int, int]:
    port, admin_port, heartbeat_port = free_port(), free_port(), free_port()
    backends = ", ".join(f'"127.0.0.1:{p}"' for p in ports.values())
    config = config_dir / f"lb-demo-{retries}.toml"
    config.write_text(
        f"backends = [{backends}]\n[listen]\nport = {port}\n"
        "[timeouts]\nbackend_connect = 0.5\nbackend_read = 2.0\n"
        "[health]\ninterval = 1.0\ntimeout = 0.5\nunhealthy_threshold = 2\nhealthy_threshold = 2\n"
        "[circuit_breaker]\nfailure_threshold = 3\ncooldown = 3.0\nhalf_open_trials = 2\n"
        f"[retries]\nmax_retries = {retries}\nbackoff_base = 0.05\n"
        "[rate_limit]\nenabled = false\n"  # all demo traffic comes from 127.0.0.1
        f"[admin]\nport = {admin_port}\n[heartbeat]\nport = {heartbeat_port}\n"
    )
    proc = subprocess.Popen([sys.executable, "-m", "lb.server", "--config", str(config)],
                            cwd=PROJECT_ROOT, stderr=subprocess.PIPE, text=True)
    wait_for_port(port)
    return proc, port, admin_port, heartbeat_port


def admin_backends(admin_port: int) -> dict[str, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", admin_port, timeout=2)
    try:
        conn.request("GET", "/backends")
        return {row["address"]: row for row in json.loads(conn.getresponse().read())["backends"]}
    finally:
        conn.close()


def run_demo(retries: int, args) -> dict:
    ports = {name: free_port() for name in NAMES}
    victim_addr = f"127.0.0.1:{ports[VICTIM]}"
    lb_log: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        lb, lb_port, admin_port, hb_port = start_lb(ports, retries, Path(tmp))
        # Collect the LB's own log lines (UP/DOWN transitions) in the background.
        threading.Thread(target=lambda: lb_log.extend(lb.stderr), daemon=True).start()
        backends = {name: start_backend(name, port, hb_port) for name, port in ports.items()}
        per_second: dict[int, Counter] = defaultdict(Counter)
        lock = threading.Lock()
        stop_at = time.monotonic() + args.duration
        t0 = time.monotonic()

        def client():
            while time.monotonic() < stop_at:
                second = int(time.monotonic() - t0)
                conn = http.client.HTTPConnection("127.0.0.1", lb_port, timeout=5)
                try:
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    resp.read()
                    key = resp.getheader("X-Backend") if resp.status == 200 else f"HTTP {resp.status}"
                except (OSError, http.client.HTTPException) as exc:
                    key = type(exc).__name__
                finally:
                    conn.close()
                with lock:
                    per_second[second][key] += 1
                time.sleep(args.think_time)

        clients = [threading.Thread(target=client, daemon=True) for _ in range(args.clients)]
        for c in clients:
            c.start()
        timeline, events = [], []
        try:
            for second in range(args.duration):
                # Sleep until the end of this second, then act / sample.
                time.sleep(max(0.0, t0 + second + 1 - time.monotonic()))
                if second + 1 == args.kill_at:
                    backends[VICTIM].send_signal(signal.SIGKILL)
                    backends[VICTIM].wait()
                    events.append((args.kill_at, f"SIGKILL backend {VICTIM}"))
                if second + 1 == args.restart_at:
                    backends[VICTIM] = start_backend(VICTIM, ports[VICTIM], hb_port)
                    events.append((args.restart_at, f"restarted backend {VICTIM}"))
                state = admin_backends(admin_port).get(victim_addr, {})
                hb = state.get("heartbeat") or {}
                timeline.append({"second": second, "healthy": state.get("healthy"),
                                 "circuit": state.get("circuit"), "hb_age_s": hb.get("age_s")})
            for c in clients:
                c.join(6)
        finally:
            for proc in backends.values():
                if proc.poll() is None:
                    stop(proc)
            stop(lb)

    rows = []
    for entry in timeline:
        counts = per_second.get(entry["second"], Counter())
        served = {name: counts.get(name, 0) for name in NAMES}
        errors = sum(v for k, v in counts.items() if k not in NAMES)
        rows.append({**entry, **served, "errors": errors,
                     "error_kinds": {k: v for k, v in counts.items() if k not in NAMES}})
    error_seconds = [r["second"] for r in rows if r["errors"]]
    after_kill = [r["second"] for r in rows if r["second"] >= args.kill_at - 1 and r[VICTIM] == 0]
    rejoined = [r["second"] for r in rows if r["second"] >= args.restart_at and r[VICTIM] > 0]
    transitions = [line.split(": ", 1)[-1].strip() for line in lb_log
                   if "is UP" in line or "is DOWN" in line]
    return {
        "retries": retries, "rows": rows, "events": events, "transitions": transitions,
        "total_ok": sum(sum(r[n] for n in NAMES) for r in rows),
        "total_errors": sum(r["errors"] for r in rows),
        "last_error_second": max(error_seconds) if error_seconds else None,
        "victim_traffic_stopped_by": after_kill[0] if after_kill else None,
        "victim_rejoined_at": rejoined[0] if rejoined else None,
    }


def format_run(run: dict, args) -> list[str]:
    title = "retries off" if run["retries"] == 0 else f"retries on (max_retries = {run['retries']})"
    lines = [f"## Run: {title}", ""]
    lines.append("| t (s) | served by a | served by b | served by c | errors | b healthy | b circuit | b heartbeat age (s) |")
    lines.append("|------:|------------:|------------:|------------:|-------:|-----------|-----------|--------------------:|")
    marks = {sec: text for sec, text in run["events"]}
    for r in run["rows"]:
        note = f" ← {marks[r['second']]}" if r["second"] in marks else ""
        lines.append(f"| {r['second']}–{r['second'] + 1}{note} | {r['a']} | {r['b']} | {r['c']} | {r['errors']} | "
                     f"{r['healthy']} | {r['circuit']} | {r['hb_age_s']} |")
    lines.append("")
    kill = args.kill_at
    if run["last_error_second"] is None:
        lines.append(f"- **No client saw an error.** {run['total_ok']} requests succeeded.")
    else:
        lines.append(f"- {run['total_errors']} failed requests out of {run['total_errors'] + run['total_ok']}; "
                     f"the last error was in second {run['last_error_second']}–{run['last_error_second'] + 1}, "
                     f"i.e. errors stopped within {run['last_error_second'] + 1 - kill} s of the kill.")
    if run["victim_rejoined_at"] is not None:
        lines.append(f"- b served traffic again from second {run['victim_rejoined_at']} "
                     f"({run['victim_rejoined_at'] - args.restart_at} s after the restart).")
    if run["transitions"]:
        lines.append("- LB log: " + "; ".join(f"`{t}`" for t in run["transitions"]))
    lines.append("")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--retries", type=int, nargs="+", default=[0, 2],
                        help="run once per value of [retries] max_retries")
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--kill-at", type=int, default=5)
    parser.add_argument("--restart-at", type=int, default=12)
    parser.add_argument("--clients", type=int, default=6)
    parser.add_argument("--think-time", type=float, default=0.02, help="pause between a client's requests")
    args = parser.parse_args()

    runs = []
    for retries in args.retries:
        print(f"--- run with max_retries = {retries}", flush=True)
        run = run_demo(retries, args)
        runs.append(run)
        print("\n".join(format_run(run, args)), flush=True)

    lines = [
        "# Failover demo: kill a backend under traffic, then restart it",
        "",
        f"Measured {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} by "
        "`python -m experiments.demo_failover`. Real measured numbers.",
        "",
        f"Setup: 3 backends (10 ms delay) with heartbeat agents; {args.clients} client threads sending GET "
        f"in a loop ({args.think_time * 1000:.0f} ms pause); backend b is SIGKILLed at t = {args.kill_at} s "
        f"and restarted at t = {args.restart_at} s. Health checks every 1 s (timeout 0.5 s, down after 2 "
        "failures, up after 2 passes); circuit breaker opens after 3 consecutive failures, 3 s cooldown, "
        "2 half-open trials. The 'b' columns are sampled from the admin API at the end of each second.",
        "",
    ]
    for run in runs:
        lines += format_run(run, args)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / "failover_demo.md"
    path.write_text("\n".join(lines))
    (RESULTS_DIR / "failover_demo.json").write_text(json.dumps(runs, indent=2) + "\n")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
