"""The load balancer's own overhead per request: the same requests sent straight
to a backend and through the LB, one at a time.

    python -m experiments.benchmark_overhead          # ~1 min; starts its own backend and LB

Setup: one backend with a 0 ms delay, so a request's time is all networking and
parsing; the LB with connection pooling (as configured for normal use: 32
workers, pool on, rate limiting and the database off so only the routing path
is timed). The client opens a new TCP connection for every request in BOTH
cases (as the load generator does), and requests go one at a time, so no
queueing is measured. Overhead = through-LB latency minus direct latency, for
each strategy.

What the difference contains: the LB accepting the client connection, parsing
the request by hand, choosing a backend, forwarding over a pooled connection,
parsing the response and sending it back, plus one extra hop on loopback. On a
real network the extra hop would add a round trip on top of this.
"""

import argparse
import http.client
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from experiments.benchmark_concurrency import RESULTS_DIR, free_port, percentile, wait_for_port
from lb.config import PROJECT_ROOT

LB_CONFIG = """\
strategy = "{strategy}"
backends = ["127.0.0.1:{backend}"]
[listen]
port = {port}
[concurrency]
workers = 32
[pool]
size = 16
[rate_limit]
enabled = false
[database]
enabled = false
[heartbeat]
port = {heartbeat}
"""
STRATEGIES = ("round_robin", "least_connections", "ml_latency", "bandit")


def time_requests(port: int, n: int) -> list[float]:
    """Latency in ms of n sequential GETs, each on a new connection."""
    times = []
    for _ in range(n):
        started = time.perf_counter()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        response = conn.getresponse()
        response.read()
        conn.close()
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} from port {port}")
        times.append((time.perf_counter() - started) * 1000)
    return times


def summary(times: list[float]) -> dict:
    ordered = sorted(times)
    return {"median_ms": statistics.median(ordered), "p95_ms": percentile(ordered, 95),
            "mean_ms": statistics.fmean(ordered)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=3000, help="per measurement")
    args = parser.parse_args()

    backend_port, heartbeat_port = free_port(), free_port()
    backend = subprocess.Popen(
        [sys.executable, "-m", "backend.app", "--port", str(backend_port), "--quiet",
         "--heartbeat", f"127.0.0.1:{heartbeat_port}"], cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL)
    results = {}
    try:
        wait_for_port(backend_port)
        time_requests(backend_port, 200)                       # warm-up
        results["direct"] = summary(time_requests(backend_port, args.requests))
        for strategy in STRATEGIES:
            with tempfile.TemporaryDirectory() as tmp:
                lb_port = free_port()
                config = Path(tmp) / "lb.toml"
                config.write_text(LB_CONFIG.format(strategy=strategy, backend=backend_port, port=lb_port,
                                                   heartbeat=heartbeat_port))
                lb = subprocess.Popen([sys.executable, "-m", "lb.server", "--config", str(config), "--quiet"],
                                      cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL)
                try:
                    wait_for_port(lb_port)
                    time.sleep(1.5)                            # first heartbeat (the ML strategy needs one)
                    time_requests(lb_port, 200)                # warm-up: pool filled, latency history known
                    results[strategy] = summary(time_requests(lb_port, args.requests))
                finally:
                    lb.terminate()
                    lb.wait(30)
            print(f"{strategy:<18} median {results[strategy]['median_ms']:.3f} ms", flush=True)
    finally:
        backend.terminate()
        backend.wait(10)

    direct = results["direct"]
    lines = [
        "# Load balancer overhead per request", "",
        f"Measured {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `python -m experiments.benchmark_overhead`. "
        "Real measured numbers; nothing estimated.", "",
        f"Setup: {platform.system()} {platform.release()} {platform.machine()}, Python {platform.python_version()}. "
        f"One backend with a 0 ms delay; {args.requests} sequential GETs per row, each on a new client connection; "
        "LB with 32 workers and connection pooling, rate limiting and the database off. "
        "Overhead = through-LB minus direct.", "",
        "| Path | Median (ms) | p95 (ms) | Mean (ms) | Median overhead (ms) |",
        "|---|---:|---:|---:|---:|",
        f"| Direct to the backend | {direct['median_ms']:.3f} | {direct['p95_ms']:.3f} | {direct['mean_ms']:.3f} | – |",
    ]
    for strategy in STRATEGIES:
        r = results[strategy]
        lines.append(f"| Through the LB, {strategy} | {r['median_ms']:.3f} | {r['p95_ms']:.3f} | {r['mean_ms']:.3f} "
                     f"| {r['median_ms'] - direct['median_ms']:+.3f} |")
    lines += ["", "Why the extra hop costs so little: both paths open one new TCP connection per request (the "
              "client's), but behind the LB the backend connection is pooled and reused, while the direct client "
              "makes the backend accept a new connection every time. The overhead is therefore the LB's parsing, "
              "routing and copying, minus the backend's connection setup it saves. The ML strategy adds its model "
              "call (~34 µs, `ml_inference.md`) on top. On loopback; across a real network the extra hop also adds "
              "a round trip.", ""]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "overhead_benchmark.md").write_text("\n".join(lines))
    (RESULTS_DIR / "overhead_benchmark.json").write_text(json.dumps(
        {"requests": args.requests, "results": results}, indent=2) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
