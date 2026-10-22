"""Benchmark: connection pooling to backends vs a new TCP connection per request.

Starts 3 test backends and one load balancer per mode as separate processes,
then runs the same closed-loop load as benchmark_concurrency (each client
sends a request, waits for the answer, sends the next). Modes:

  - no-pool:  [pool] size = 0, LB opens and closes a backend connection per request
  - pool-N:   [pool] size = N, LB reuses keep-alive connections

Clients still open a NEW connection to the LB for every request in both modes
(the LB does not support client-side keep-alive), so only the LB -> backend
hop differs. Measured per run: throughput, average and p95 latency, backend
connections the LB opened (from the admin API's pool counters), and sockets
left in TIME_WAIT on the backend ports.

Also measures the raw cost of one TCP connect + close on loopback, to show
what the pool saves per request before any HTTP is involved.

Run:  python -m experiments.benchmark_pooling
      python -m experiments.benchmark_pooling --levels 1 8 --duration 3 --repeats 1
"""

import argparse
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import http.client
from datetime import datetime, timezone
from pathlib import Path

from experiments.benchmark_concurrency import (
    PROJECT_ROOT, RESULTS_DIR, free_port, raise_fd_limit, run_load, start_backends, stop,
    wait_for_port, wait_for_time_wait_to_clear,
)


def start_lb(pool_size: int, backend_ports: list[int], workers: int, config_dir: Path) -> tuple[subprocess.Popen, int, int]:
    port, admin_port = free_port(), free_port()
    backends = ", ".join(f'"127.0.0.1:{p}"' for p in backend_ports)
    config = config_dir / f"lb-pool-{pool_size}.toml"
    config.write_text(
        f"backends = [{backends}]\n[listen]\nport = {port}\n"
        f"[concurrency]\nworkers = {workers}\nqueue_size = 128\nshutdown_timeout = 5\n"
        f"[pool]\nsize = {pool_size}\n"
        # Everything else held constant and out of the way: all load comes
        # from one IP, and a retry would hide or add backend connections.
        "[retries]\nmax_retries = 0\n[rate_limit]\nenabled = false\n"
        f"[admin]\nport = {admin_port}\n"
    )
    proc = subprocess.Popen([sys.executable, "-m", "lb.server", "--config", str(config), "--quiet"],
                            cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL)
    wait_for_port(port)
    wait_for_port(admin_port)
    return proc, port, admin_port


def pool_counters(admin_port: int) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", admin_port, timeout=5)
    try:
        conn.request("GET", "/backends")
        return json.loads(conn.getresponse().read())["pool"]
    finally:
        conn.close()


def time_wait_on_ports(ports: list[int]) -> int:
    """Sockets in TIME_WAIT whose local or remote port is one of the backends'."""
    try:  # Linux
        out = subprocess.run(["ss", "-tan", "state", "time-wait"], capture_output=True, text=True).stdout
        suffixes = tuple(f":{p}" for p in ports)
    except FileNotFoundError:  # macOS prints addresses as 127.0.0.1.9001
        out = subprocess.run(["netstat", "-an", "-p", "tcp"], capture_output=True, text=True).stdout
        out = "\n".join(line for line in out.splitlines() if "TIME_WAIT" in line)
        suffixes = tuple(f".{p}" for p in ports)
    return sum(any(field.endswith(suffixes) for field in line.split()) for line in out.splitlines())


def handshake_cost(port: int, count: int) -> dict:
    """Time socket() + connect() (the 3-way handshake) + close() to a backend on loopback."""
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        connected = time.perf_counter()
        sock.close()
        samples.append((connected - started) * 1e6)
    samples.sort()
    return {"count": count, "avg_us": round(statistics.fmean(samples), 1),
            "median_us": round(samples[len(samples) // 2], 1)}


def summarise(runs: list[dict]) -> dict:
    def mean(key):
        values = [r[key] for r in runs if r[key] is not None]
        return round(statistics.fmean(values), 1) if values else None
    return {
        "throughput_rps": mean("throughput_rps"),
        "avg_latency_ms": mean("avg_latency_ms"),
        "p95_latency_ms": mean("p95_latency_ms"),
        "backend_conns_per_1000_requests": mean("backend_conns_per_1000_requests"),
        "time_wait_backend_ports": mean("time_wait_backend_ports"),
        "errors": sum(r["errors"] for r in runs),
    }


def write_markdown(report: dict, path: Path) -> None:
    s, h = report["setup"], report["handshake"]
    lines = [
        "# Connection pooling vs a new backend connection per request",
        "",
        f"Measured {report['measured_at']} by `python -m experiments.benchmark_pooling`. "
        "Real measured numbers; nothing estimated.",
        "",
        "## Setup",
        "",
        f"- Machine: {s['machine']}, {s['cpu_count']} logical CPUs, Python {s['python']}",
        f"- {s['backends']} backend processes (`backend/app.py`, {s['backend_delay_ms']} ms artificial delay), "
        f"LB with {s['workers']} workers, all on loopback",
        f"- Modes: `no-pool` = `[pool] size = 0`; `pool-{s['pool_size']}` = `[pool] size = {s['pool_size']}`. "
        "Retries and rate limiting off in both.",
        f"- Closed-loop clients, {s['duration_s']} s per run, {s['repeats']} run(s) per cell, mean shown. "
        "Clients open a new connection to the LB per request in BOTH modes; only LB -> backend differs.",
        "- Backend connections per 1000 requests: from the LB's pool counters (admin API), "
        "i.e. how many TCP handshakes the LB made to backends",
        "- TIME_WAIT: sockets on the backend ports in TIME_WAIT right after each run",
        "",
        "## Raw cost of one TCP connection on loopback",
        "",
        f"`socket.create_connection()` to a backend, {h['count']} times: average {h['avg_us']} µs, "
        f"median {h['median_us']} µs for socket + 3-way handshake (close excluded). On loopback the "
        "round trip is almost free; across a real network each handshake adds one full RTT "
        "(often 0.5-50 ms), which is where pooling matters most.",
        "",
        "## Results",
        "",
        "| Clients | Mode | Throughput (req/s) | Avg latency (ms) | p95 latency (ms) | Backend conns / 1000 req | TIME_WAIT on backend ports | Errors |",
        "|--------:|------|-------------------:|-----------------:|-----------------:|-------------------------:|---------------------------:|-------:|",
    ]
    for level in report["results"]:
        for mode in report["modes"]:
            m = level[mode]["summary"]
            lines.append(
                f"| {level['clients']} | {mode} | {m['throughput_rps']} | {m['avg_latency_ms']} | "
                f"{m['p95_latency_ms']} | {m['backend_conns_per_1000_requests']} | "
                f"{m['time_wait_backend_ports']} | {m['errors']} |"
            )
    lines += ["", "Per-run numbers are in `pooling_benchmark.json`.", ""]
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--pool-size", type=int, default=32)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--backends", type=int, default=3)
    parser.add_argument("--delay-ms", type=float, default=0)
    parser.add_argument("--pause", type=float, default=2.0)
    args = parser.parse_args()

    fd_limit = raise_fd_limit()
    backend_procs, backend_ports = start_backends(args.backends, args.delay_ms)
    modes = {"no-pool": 0, f"pool-{args.pool_size}": args.pool_size}
    results = [{"clients": level} for level in args.levels]
    try:
        handshake = handshake_cost(backend_ports[0], 2000)
        print(f"loopback connect: avg {handshake['avg_us']} us, median {handshake['median_us']} us", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            for mode, size in modes.items():
                lb, port, admin_port = start_lb(size, backend_ports, args.workers, Path(tmp))
                try:
                    run_load(port, 4, 1.0)  # warm-up (fills the pool), not recorded
                    for level, row in zip(args.levels, results):
                        runs = []
                        for i in range(args.repeats):
                            time.sleep(args.pause)
                            wait_for_time_wait_to_clear()
                            before = pool_counters(admin_port)
                            run = run_load(port, level, args.duration)
                            after = pool_counters(admin_port)
                            # Health checks open their own connections, outside the pool,
                            # so `created` counts only proxied traffic.
                            opened = after["created"] - before["created"]
                            total = run["requests_ok"] + run["errors"]
                            run["backend_conns_opened"] = opened
                            run["backend_conns_per_1000_requests"] = round(1000 * opened / total, 1) if total else None
                            run["time_wait_backend_ports"] = time_wait_on_ports(backend_ports)
                            runs.append(run)
                            print(f"{mode:>8} clients={level:<3} run {i + 1}: {run['throughput_rps']:>7} req/s  "
                                  f"avg {run['avg_latency_ms']} ms  p95 {run['p95_latency_ms']} ms  "
                                  f"backend conns {opened}/{total}  TIME_WAIT {run['time_wait_backend_ports']}  "
                                  f"errors {run['errors']}", flush=True)
                        row[mode] = {"runs": runs, "summary": summarise(runs)}
                finally:
                    stop(lb)
    finally:
        for proc in backend_procs:
            stop(proc)

    report = {
        "modes": list(modes),
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "setup": {
            "machine": f"{platform.system()} {platform.release()} {platform.machine()}",
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "backends": args.backends,
            "backend_delay_ms": args.delay_ms,
            "workers": args.workers,
            "pool_size": args.pool_size,
            "duration_s": args.duration,
            "repeats": args.repeats,
            "fd_limit": fd_limit,
        },
        "handshake": handshake,
        "results": results,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "pooling_benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    write_markdown(report, RESULTS_DIR / "pooling_benchmark.md")
    print(f"saved {RESULTS_DIR / 'pooling_benchmark.md'}")


if __name__ == "__main__":
    main()
