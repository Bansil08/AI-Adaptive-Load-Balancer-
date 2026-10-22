"""Multithreaded HTTP load generator with a configurable request rate.

Open loop: request i is sent at its scheduled time whether or not earlier ones
have finished, the way independent users arrive. A closed-loop generator
("send, wait for the answer, send the next") slows itself down when the server
slows down, so it sends less load exactly when things go wrong and reports
latencies that look better than reality; this is called coordinated omission.

  - A scheduler thread computes send times from a rate profile (e.g. 30 req/s
    for 10 s, then 180 req/s) using Poisson arrivals from a SEEDED random
    generator: the same seed gives the same arrival sequence every run.
  - A pool of worker threads sends the requests (each on a new connection,
    like independent clients) and records latency, status and which backend
    answered (the X-Backend header).
  - Latency is measured from the SCHEDULED send time, not from when a worker
    got to it, so a backlog on the client side is counted too.

Run:  python -m experiments.load_generator --port 8080 --rate 50 --duration 10
"""

import argparse
import http.client
import queue
import random
import statistics
import threading
import time
from dataclasses import dataclass
from typing import Callable

from experiments.benchmark_concurrency import percentile


@dataclass(frozen=True)
class Result:
    scheduled_s: float        # when it was meant to be sent, seconds after the start
    latency_ms: float         # from the scheduled time to the full response (or the error)
    status: int | None        # HTTP status, or None if no HTTP answer at all
    error: str | None         # exception name when status is None
    backend: str | None       # X-Backend header: which backend served it


RateProfile = Callable[[float], float]  # seconds since start -> requests per second


def step_profile(steps: list[tuple[float, float]]) -> RateProfile:
    """[(0, 30), (10, 180)] means 30 req/s from t=0 and 180 req/s from t=10."""
    ordered = sorted(steps)

    def rate(t: float) -> float:
        current = ordered[0][1]
        for start, value in ordered:
            if t >= start:
                current = value
        return current
    return rate


def schedule(profile: RateProfile, duration: float, seed: int) -> list[float]:
    """Poisson arrival times in [0, duration): gaps are exponential with mean 1/rate."""
    rng = random.Random(seed)
    times, t = [], 0.0
    while True:
        rate = profile(t)
        t += rng.expovariate(rate) if rate > 0 else 0.1
        if t >= duration:
            return times
        if rate > 0:
            times.append(t)


class LoadGenerator:
    def __init__(self, host: str, port: int, path: str = "/", timeout: float = 30.0, workers: int = 512):
        self.host, self.port, self.path = host, port, path
        self.timeout = timeout
        self.workers = workers

    def run(self, profile: RateProfile, duration: float, seed: int = 42,
            events: list[tuple[float, Callable[[], None]]] = ()) -> tuple[list[Result], float]:
        """Send load for `duration` seconds; call each event callback at its time.

        Returns (results ordered by schedule, the time.time() the run started at).
        """
        send_times = schedule(profile, duration, seed)
        jobs: queue.Queue = queue.Queue()
        results: list[Result | None] = [None] * len(send_times)
        threads = [threading.Thread(target=self._worker, args=(jobs, results), daemon=True)
                   for _ in range(min(self.workers, max(1, len(send_times))))]
        for t in threads:
            t.start()
        pending_events = sorted(events, key=lambda e: e[0])
        started_wall = time.time()
        started = time.monotonic()
        for i, offset in enumerate(send_times):
            while pending_events and pending_events[0][0] <= offset:
                at, action = pending_events.pop(0)
                self._sleep_until(started + at)
                action()  # e.g. kill a backend; runs on this thread, so keep it short
            self._sleep_until(started + offset)
            jobs.put((i, started + offset, offset))
        for at, action in pending_events:  # events scheduled after the last request
            self._sleep_until(started + at)
            action()
        for _ in threads:
            jobs.put(None)
        for t in threads:
            t.join()
        return [r for r in results if r is not None], started_wall

    @staticmethod
    def _sleep_until(deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def _worker(self, jobs: queue.Queue, results: list) -> None:
        while (job := jobs.get()) is not None:
            i, scheduled, offset = job
            status = error = backend = None
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            try:
                conn.request("GET", self.path)
                response = conn.getresponse()
                response.read()
                status, backend = response.status, response.getheader("X-Backend")
            except (OSError, http.client.HTTPException) as exc:
                error = type(exc).__name__
            finally:
                conn.close()
            results[i] = Result(offset, (time.monotonic() - scheduled) * 1000, status, error, backend)


def summarise(results: list[Result], duration: float) -> dict:
    """Client-side numbers: latency over successful requests, errors over all."""
    ok = sorted(r.latency_ms for r in results if r.status is not None and r.status < 500)
    failed = sum(1 for r in results if r.status is None or r.status >= 500)
    p95 = percentile(ok, 95) if ok else None
    return {
        "sent": len(results),
        "ok": len(ok),
        "error_rate": failed / len(results) if results else 0.0,
        "avg_latency_ms": statistics.fmean(ok) if ok else None,
        "p95_latency_ms": p95,
        "throughput_rps": len(ok) / duration,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--rate", type=float, default=50, help="requests per second")
    parser.add_argument("--duration", type=float, default=10, help="seconds")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    results, _ = LoadGenerator(args.host, args.port).run(step_profile([(0, args.rate)]), args.duration, args.seed)
    s = summarise(results, args.duration)
    print(f"sent {s['sent']}, ok {s['ok']}, error rate {s['error_rate']:.2%}, "
          f"avg {s['avg_latency_ms'] or 0:.1f} ms, p95 {s['p95_latency_ms'] or 0:.1f} ms, "
          f"throughput {s['throughput_rps']:.1f} req/s")


if __name__ == "__main__":
    main()
