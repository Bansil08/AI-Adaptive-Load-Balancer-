"""Benchmark: thread pool vs thread-per-connection.

Starts 3 test backends and the load balancer as separate processes, then runs
closed-loop clients: each of C client threads sends one request, waits for
the response, and immediately sends the next, for a fixed duration. Measured
per (mode, concurrency) run:

  - throughput:     successful responses per second
  - latency:        average and p95 over successful requests, as the client sees it
  - errors:         failed connections / non-200 responses
  - peak threads:   the LB process's OS thread count, sampled every 50 ms

The clients run in this process and the LB in its own, so the two don't
compete for one GIL. Results go to experiments/results/concurrency_benchmark.{json,md}.

Run:  python -m experiments.benchmark_concurrency
      python -m experiments.benchmark_concurrency --levels 10 50 --duration 3 --repeats 1
"""

import argparse
import http.client
import json
import math
import os
import platform
import resource
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "experiments" / "results"
SAMPLE_INTERVAL = 0.05
# Each request opens two TCP connections (client->LB, LB->backend), and every
# closed connection sits in TIME_WAIT for 2*MSL (30 s on macOS) holding its
# ephemeral port; there are only ~16k of those. Without waiting for them to
# clear, a run inherits the previous run's exhaustion and fails with
# EADDRNOTAVAIL, which measures the leftovers, not the mode under test.
TIME_WAIT_CLEAR_BELOW = 1000
TIME_WAIT_MAX_WAIT_S = 90
BASELINE = "thread-per-connection"


# --- processes -------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # A bare connect + close; the LB treats it as a client that sent nothing.
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"nothing listening on port {port}")


def start_backends(count: int, delay_ms: float) -> tuple[list[subprocess.Popen], list[int]]:
    ports = [free_port() for _ in range(count)]
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "backend.app", "--port", str(port), "--name", f"b{i}",
             "--delay-ms", str(delay_ms), "--quiet"],
            cwd=PROJECT_ROOT, stdout=subprocess.DEVNULL,
        )
        for i, port in enumerate(ports)
    ]
    for port in ports:
        wait_for_port(port)
    return procs, ports


def start_lb(mode: str, backend_ports: list[int], workers: int, queue_size: int,
             config_dir: Path) -> tuple[subprocess.Popen, int]:
    """mode is "pool-<workers>" or BASELINE."""
    port = free_port()
    config = config_dir / f"lb-{mode}.toml"
    backends = ", ".join(f'"127.0.0.1:{p}"' for p in backend_ports)
    config.write_text(
        f"backends = [{backends}]\n[listen]\nport = {port}\n"
        f"[concurrency]\nworkers = {workers}\nqueue_size = {queue_size}\nshutdown_timeout = 5\n"
        # Keep the Phase 2 setup so results stay comparable: no pooling, no
        # retries, and no rate limit (all load comes from one IP, 127.0.0.1).
        "[pool]\nsize = 0\n[retries]\nmax_retries = 0\n[rate_limit]\nenabled = false\n"
    )
    args = [sys.executable, "-m", "lb.server", "--config", str(config), "--quiet"]
    if mode == BASELINE:
        args.append("--thread-per-connection")
    # Errors are counted on the client side; the LB's per-error log lines would only flood the terminal.
    proc = subprocess.Popen(args, cwd=PROJECT_ROOT, stderr=subprocess.DEVNULL)
    wait_for_port(port)
    return proc, port


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def os_thread_count(pid: int) -> int:
    """Threads in process `pid` as the OS sees them (not just Python's threading view)."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():  # Linux
        for line in status.read_text().splitlines():
            if line.startswith("Threads:"):
                return int(line.split()[1])
    # macOS: `ps -M` prints a header line, then one line per thread.
    out = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout
    return max(0, len(out.strip().splitlines()) - 1)


def time_wait_count() -> int:
    """TCP connections in TIME_WAIT on this machine."""
    try:  # Linux
        out = subprocess.run(["ss", "-tan", "state", "time-wait"], capture_output=True, text=True).stdout
        return max(0, len(out.splitlines()) - 1)
    except FileNotFoundError:  # macOS
        out = subprocess.run(["netstat", "-an", "-p", "tcp"], capture_output=True, text=True).stdout
        return out.count("TIME_WAIT")


def wait_for_time_wait_to_clear() -> int:
    deadline = time.monotonic() + TIME_WAIT_MAX_WAIT_S
    while (count := time_wait_count()) >= TIME_WAIT_CLEAR_BELOW and time.monotonic() < deadline:
        time.sleep(2)
    return count


class ThreadSampler:
    """Background thread recording the peak OS thread count of a process."""

    def __init__(self, pid: int):
        self.pid = pid
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, os_thread_count(self.pid))
            self._stop.wait(SAMPLE_INTERVAL)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


# --- load ------------------------------------------------------------------------


def run_load(port: int, concurrency: int, duration: float) -> dict:
    """Closed-loop load: `concurrency` clients, each sending back-to-back requests."""
    latencies: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    start_line = threading.Barrier(concurrency + 1)
    end_at = [0.0]

    def client():
        mine, my_errors = [], []
        start_line.wait()
        try:
            while time.monotonic() < end_at[0]:
                started = time.perf_counter()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    resp.read()
                    if resp.status == 200:
                        mine.append((time.perf_counter() - started) * 1000)
                    else:
                        my_errors.append(f"HTTP {resp.status}")
                # HTTPException covers IncompleteRead / BadStatusLine, which are not OSErrors.
                except (OSError, http.client.HTTPException) as exc:
                    my_errors.append(type(exc).__name__)
                finally:
                    conn.close()
        finally:
            # Merge even if something unexpected kills this thread, so its
            # measurements are never silently dropped.
            with lock:
                latencies.extend(mine)
                errors.extend(my_errors)

    threads = [threading.Thread(target=client, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    began = time.monotonic()
    end_at[0] = began + duration
    start_line.wait()
    for t in threads:
        t.join()
    # Requests in flight at the deadline finish a little late; divide by the real span.
    elapsed = time.monotonic() - began

    latencies.sort()
    error_kinds: dict[str, int] = {}
    for e in errors:
        error_kinds[e] = error_kinds.get(e, 0) + 1
    return {
        "requests_ok": len(latencies),
        "errors": len(errors),
        "error_kinds": error_kinds,
        "elapsed_s": round(elapsed, 3),
        "throughput_rps": round(len(latencies) / elapsed, 1),
        "avg_latency_ms": round(statistics.fmean(latencies), 1) if latencies else None,
        "p95_latency_ms": round(percentile(latencies, 95), 1) if latencies else None,
    }


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value with at least p% of samples at or below it."""
    rank = math.ceil(p / 100 * len(sorted_values))
    return sorted_values[max(rank, 1) - 1]


# --- report ----------------------------------------------------------------------


def raise_fd_limit() -> int:
    """Each in-flight request costs fds in this process and the LB; children inherit the limit."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 16384 if hard == resource.RLIM_INFINITY else min(hard, 16384)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    return resource.getrlimit(resource.RLIMIT_NOFILE)[0]


def summarise(runs: list[dict]) -> dict:
    def mean(key):
        values = [r[key] for r in runs if r[key] is not None]
        return round(statistics.fmean(values), 1) if values else None
    return {
        "throughput_rps": mean("throughput_rps"),
        "avg_latency_ms": mean("avg_latency_ms"),
        "p95_latency_ms": mean("p95_latency_ms"),
        "peak_threads": max(r["peak_threads"] for r in runs),
        "errors": sum(r["errors"] for r in runs),
    }


def write_markdown(report: dict, path: Path) -> None:
    s = report["setup"]
    lines = [
        "# Thread pool vs thread-per-connection",
        "",
        f"Measured {report['measured_at']} by `python -m experiments.benchmark_concurrency`. "
        "Real measured numbers; nothing estimated.",
        "",
        "## Setup",
        "",
        f"- Machine: {s['machine']}, {s['cpu_count']} logical CPUs, Python {s['python']}",
        f"- {s['backends']} backend processes, {s['backend_delay_ms']} ms artificial delay each "
        "(I/O-bound: they sleep)",
        f"- Thread pool sizes: {', '.join(map(str, s['workers']))} workers, queue size "
        f"{s['queue_size']}; listen backlog 128",
        f"- Closed-loop clients (new TCP connection per request), {s['duration_s']} s per run, "
        f"{s['repeats']} run(s) per cell; table shows the mean over runs (peak threads: max)",
        f"- Peak threads = OS thread count of the LB process, sampled every {int(SAMPLE_INTERVAL * 1000)} ms",
        f"- Before each run the script waits until fewer than {TIME_WAIT_CLEAR_BELOW} sockets are in "
        "TIME_WAIT, so a run doesn't inherit the previous run's ephemeral-port usage",
        "",
        "## Results",
        "",
        "| Clients | Mode | Throughput (req/s) | Avg latency (ms) | p95 latency (ms) | Peak LB threads | Errors |",
        "|--------:|------|-------------------:|-----------------:|-----------------:|----------------:|-------:|",
    ]
    for level in report["results"]:
        for mode in report["modes"]:
            m = level[mode]["summary"]
            lines.append(
                f"| {level['clients']} | {mode} | {m['throughput_rps']} | {m['avg_latency_ms']} | "
                f"{m['p95_latency_ms']} | {m['peak_threads']} | {m['errors']} |"
            )
    lines += ["", "Per-run numbers are in `concurrency_benchmark.json`.", ""]
    path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--levels", type=int, nargs="+", default=[10, 50, 200, 500],
                        help="numbers of concurrent clients to test")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per run")
    parser.add_argument("--repeats", type=int, default=3, help="runs per (mode, level)")
    parser.add_argument("--workers", type=int, nargs="+", default=[32, 256],
                        help="pool sizes to test (each is compared with thread-per-connection)")
    parser.add_argument("--queue-size", type=int, default=128)
    parser.add_argument("--backends", type=int, default=3)
    parser.add_argument("--delay-ms", type=float, default=50)
    parser.add_argument("--pause", type=float, default=2.0,
                        help="seconds between runs so closed connections settle")
    args = parser.parse_args()

    fd_limit = raise_fd_limit()
    backend_procs, backend_ports = start_backends(args.backends, args.delay_ms)
    results = [{"clients": level} for level in args.levels]
    modes = {f"pool-{w}": w for w in args.workers} | {BASELINE: 0}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for mode, workers in modes.items():
                # workers is ignored in BASELINE mode; any valid value will do.
                lb, port = start_lb(mode, backend_ports, workers or 1, args.queue_size, Path(tmp))
                try:
                    run_load(port, 5, 1.0)  # warm-up, not recorded
                    for level, row in zip(args.levels, results):
                        runs = []
                        for i in range(args.repeats):
                            time.sleep(args.pause)
                            time_wait = wait_for_time_wait_to_clear()
                            with ThreadSampler(lb.pid) as sampler:
                                run = run_load(port, level, args.duration)
                            run["peak_threads"] = sampler.peak
                            run["time_wait_at_start"] = time_wait
                            runs.append(run)
                            print(f"{mode:>22} clients={level:<4} run {i + 1}: "
                                  f"{run['throughput_rps']:>7} req/s  avg {run['avg_latency_ms']} ms  "
                                  f"p95 {run['p95_latency_ms']} ms  threads {run['peak_threads']}  "
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
            "queue_size": args.queue_size,
            "duration_s": args.duration,
            "repeats": args.repeats,
            "fd_limit": fd_limit,
        },
        "results": results,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "concurrency_benchmark.json").write_text(json.dumps(report, indent=2) + "\n")
    write_markdown(report, RESULTS_DIR / "concurrency_benchmark.md")
    print(f"saved {RESULTS_DIR / 'concurrency_benchmark.md'}")


if __name__ == "__main__":
    main()
