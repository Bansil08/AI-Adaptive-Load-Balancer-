"""Background demo load, started and stopped from the dashboard.

The same open-loop idea as experiments/load_generator.py: requests leave on a
Poisson schedule whether or not earlier ones have answered, so a slow backend
shows up as latency instead of quietly slowing the load down (coordinated
omission). Unlike the experiment generator it runs until stopped and its rate
can change while it runs, which is what a live demo needs; it records only
counters, not every request (the LB's request log in Postgres has those).

Threads: one scheduler sleeps until each send time and puts a job on a
bounded queue; a fixed pool of workers sends the requests. If the LB is so
slow that every worker is busy and the queue is full, extra jobs are skipped
and counted, rather than piling up without limit.
"""

import http.client
import queue
import random
import threading
import time
from urllib.parse import urlsplit

WORKERS = 64
MAX_RATE = 200.0


class DemoLoad:
    def __init__(self, url: str):
        parts = urlsplit(url)
        self.host, self.port = parts.hostname or "127.0.0.1", parts.port or 80
        self._lock = threading.Lock()   # protects the counters and the running state
        self._jobs: queue.Queue = queue.Queue(maxsize=WORKERS * 4)
        self._stop: threading.Event | None = None
        self.rate = 0.0
        self.sent = self.ok = self.errors = self.skipped = 0

    def status(self) -> dict:
        with self._lock:
            return {"running": self._stop is not None, "rate": self.rate, "sent": self.sent, "ok": self.ok,
                    "errors": self.errors, "skipped": self.skipped}

    def start(self, rate: float) -> None:
        """Start, or change the rate of, the background load."""
        if not 0 < rate <= MAX_RATE:
            raise ValueError(f"rate must be in (0, {MAX_RATE:g}] requests per second")
        with self._lock:
            self.rate = rate               # the running scheduler picks it up at its next send
            if self._stop is not None:
                return
            self._stop = stop = threading.Event()
            self.sent = self.ok = self.errors = self.skipped = 0
        threading.Thread(target=self._schedule, args=(stop,), name="demo-load", daemon=True).start()
        for i in range(WORKERS):
            threading.Thread(target=self._work, args=(stop,), name=f"demo-load-{i}", daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            stop, self._stop, self.rate = self._stop, None, 0.0
        if stop is not None:
            stop.set()
        # Throw away jobs nobody sent yet: otherwise the next start() would fire
        # them all at once, a burst the chosen rate never asked for.
        while True:
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break

    def _schedule(self, stop: threading.Event) -> None:
        rng = random.Random()
        next_at = time.monotonic()
        while not stop.is_set():
            with self._lock:
                rate = self.rate
            if rate <= 0:
                return
            next_at += rng.expovariate(rate)       # exponential gaps = Poisson arrivals
            delay = next_at - time.monotonic()
            if delay < -1.0:
                # Over a second behind (the machine slept, or this thread was
                # starved): skip the missed sends rather than fire them in a burst.
                next_at, delay = time.monotonic(), 0.0
            if delay > 0 and stop.wait(delay):
                return
            try:
                self._jobs.put_nowait(None)
            except queue.Full:
                with self._lock:
                    self.skipped += 1

    def _work(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
            try:
                conn.request("GET", "/")
                response = conn.getresponse()
                response.read()
                ok = response.status < 500
            except (OSError, http.client.HTTPException):
                ok = False
            finally:
                conn.close()
            with self._lock:
                self.sent += 1
                if ok:
                    self.ok += 1
                else:
                    self.errors += 1
