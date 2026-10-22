"""Test backend HTTP server with configurable artificial delay and a /health endpoint.

Run:  python -m backend.app --port 9001 --name fast --delay-ms 20
      python -m backend.app --port 9001 --heartbeat 127.0.0.1:9100   # + UDP heartbeats to the LB
      python -m backend.app --port 9001 --capacity 8 --allow-control  # experiments (Phase 5)

--capacity N: at most N requests are processed at once and the rest wait, like
a server with N worker threads. Without it a sleep()-based backend has
unlimited capacity: more traffic never makes it slower, so a traffic spike or
an overloaded server could not be simulated.

--allow-control: enables two runtime controls, both off by default, because
requests reach backends THROUGH the load balancer: with them on, any client
could slow a backend down or take it out.
  POST /control {"delay_ms": 300}             set the delay (the experiments' sudden slowdown)
  POST /fault   {"type": "slow", "ms": 150}   fault injection for the live demo (dashboard):
                {"type": "crash"}             stop answering: every connection is closed
                                              without a response, /health included
                {"type": "restore"}           back to the start-up delay, answering again
  GET  /fault                                 the current fault
With --control-token (or the ADMIN_TOKEN environment variable) set, a POST
must also carry the header "X-Admin-Token: <token>".
"""

import argparse
import hmac
import json
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAULT_TYPES = ("slow", "crash", "restore")


class BackendServer(ThreadingHTTPServer):
    """http.server with a thread per request, plus the delay settings."""

    daemon_threads = True
    # socketserver's default listen backlog is only 5: with more simultaneous
    # connects the kernel drops the extra SYNs and clients stall for a ~1 s
    # retransmit. Match the load balancer's backlog so the backend isn't the bottleneck.
    request_queue_size = 128

    def __init__(self, address, name: str, delay_ms: float = 0, jitter_ms: float = 0, quiet: bool = False,
                 capacity: int = 0, allow_control: bool = False, seed: int | None = None,
                 control_token: str = ""):
        super().__init__(address, BackendHandler)
        self.name = name
        self.delay_ms = delay_ms
        self.jitter_ms = jitter_ms
        # Start-up values, so "restore" undoes any injected fault.
        self.base_delay_ms = delay_ms
        self.base_jitter_ms = jitter_ms
        self.fault = "none"       # "none", "slow" or "crash" (one attribute store: atomic under the GIL)
        self.control_token = control_token
        self.quiet = quiet
        self.allow_control = allow_control
        self.capacity = capacity
        # A counting semaphore with N permits: the (N+1)th concurrent request
        # blocks in acquire() until one finishes. None = unlimited.
        self._slots = threading.BoundedSemaphore(capacity) if capacity > 0 else None
        # Seeded so the jitter sequence is the same on every run of an experiment.
        self.rng = random.Random(seed)
        # Requests being handled right now, reported in heartbeats. Each request
        # runs in its own thread, so the counter needs a lock (+= is not atomic).
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    def handle_error(self, request, client_address):
        # A client that disconnects or resets mid-request is normal (the LB's
        # health checks and lingering close do it). socketserver would print a
        # full traceback for each; only report real errors.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)

    def in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    def _add_in_flight(self, delta: int) -> None:
        with self._in_flight_lock:
            self._in_flight += delta

    def work(self) -> float:
        """Simulate one request's work; returns the delay used (ms), excluding time queued for a slot."""
        delay_ms = self.delay_ms + self.rng.uniform(0, self.jitter_ms)
        if self._slots is None:
            time.sleep(delay_ms / 1000)
            return delay_ms
        with self._slots:  # wait for a free "worker", then hold it while working
            time.sleep(delay_ms / 1000)
        return delay_ms


class BackendHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 so every response carries Content-Length (needed for keep-alive,
    # which Phase 3's connection pooling relies on).
    protocol_version = "HTTP/1.1"
    # TCP_NODELAY (Nagle off). http.server sends the headers and the body as two
    # writes; with Nagle on, the body waits until the headers are ACKed, and the
    # receiver delays that ACK (up to ~40 ms) hoping to piggyback it. On a fresh
    # connection Linux ACKs the first packets at once, so this never showed with
    # a new connection per request; on a pooled keep-alive connection it stalled
    # every response (measured: 71 ms instead of 25 ms through the LB to the
    # Docker backends). nginx, Go and Node set TCP_NODELAY by default.
    disable_nagle_algorithm = True
    server: BackendServer

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle(send_body=False)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

    def _handle(self, send_body: bool = True) -> None:
        if self.server.fault == "crash" and self.path != "/fault":
            # A crashed server: close without answering (the LB sees no response,
            # like a process that died mid-request). /fault stays reachable so the
            # dashboard can restore it.
            self.close_connection = True
            return
        self.server._add_in_flight(1)
        try:
            self._respond(send_body)
        finally:
            self.server._add_in_flight(-1)

    def _respond(self, send_body: bool) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(length) if length else b""

        status = 200
        if self.path == "/health":
            # No artificial delay and no capacity queue: a health check asks
            # "is the process alive?", not "is it fast?".
            payload = {"status": "ok", "server": self.server.name}
        elif self.path == "/control" and self.command == "POST":
            status, payload = self._control(request_body)
        elif self.path == "/fault":
            status, payload = self._fault(request_body)
        else:
            # sleep() simulates I/O-bound latency (e.g. waiting on a database);
            # --capacity adds queueing once N requests are already being worked on.
            delay_ms = self.server.work()
            payload = {
                "server": self.server.name,
                "method": self.command,
                "path": self.path,
                "delay_ms": round(delay_ms, 1),
                "body_bytes": len(request_body),
                "x_forwarded_for": self.headers.get("X-Forwarded-For"),
            }

        body = json.dumps(payload).encode() + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Backend", self.server.name)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _authorised(self) -> tuple[int, dict] | None:
        """None if this control request may proceed, else the error response."""
        if not self.server.allow_control:
            return 404, {"error": "control endpoints disabled (start with --allow-control)"}
        token = self.server.control_token
        # compare_digest takes the same time however many leading characters
        # match, so the token can't be guessed one character at a time.
        if token and not hmac.compare_digest(self.headers.get("X-Admin-Token", "").encode(), token.encode()):
            return 403, {"error": "missing or wrong X-Admin-Token"}
        return None

    def _fault(self, request_body: bytes) -> tuple[int, dict]:
        server = self.server
        if self.command != "GET":
            if (refused := self._authorised()) is not None:
                return refused
            try:
                change = json.loads(request_body or b"{}")
                kind = change.get("type") if isinstance(change, dict) else None
                if kind not in FAULT_TYPES:
                    raise ValueError(f'body must be {{"type": one of {list(FAULT_TYPES)}}}')
                if kind == "slow":
                    ms = float(change.get("ms", 150))
                    if not 0 < ms <= 60_000:
                        raise ValueError("ms out of range")
                    server.delay_ms = server.base_delay_ms + ms
                    server.fault = "slow"
                elif kind == "crash":
                    server.fault = "crash"
                else:
                    server.delay_ms, server.jitter_ms = server.base_delay_ms, server.base_jitter_ms
                    server.fault = "none"
            except (ValueError, TypeError) as exc:
                return 400, {"error": str(exc)}
        return 200, {"server": server.name, "fault": server.fault, "delay_ms": server.delay_ms,
                     "base_delay_ms": server.base_delay_ms}

    def _control(self, request_body: bytes) -> tuple[int, dict]:
        if (refused := self._authorised()) is not None:
            return refused
        try:
            changes = json.loads(request_body or b"{}")
            if not isinstance(changes, dict):
                raise ValueError('body must be a JSON object like {"delay_ms": 300}')
            for key in ("delay_ms", "jitter_ms"):
                if key in changes:
                    value = float(changes[key])
                    if not 0 <= value <= 60_000:
                        raise ValueError(f"{key} out of range")
                    setattr(self.server, key, value)  # one attribute store: atomic under the GIL
        except (ValueError, TypeError, AttributeError) as exc:
            return 400, {"error": str(exc)}
        return 200, {"server": self.server.name, "delay_ms": self.server.delay_ms,
                     "jitter_ms": self.server.jitter_ms}

    def log_message(self, format, *args):
        if not self.server.quiet:
            super().log_message(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test backend server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--name", help="name reported in responses (default: backend-<port>)")
    parser.add_argument("--delay-ms", type=float, default=0, help="fixed delay added to every request")
    parser.add_argument("--jitter-ms", type=float, default=0, help="extra random delay in [0, jitter]")
    parser.add_argument("--quiet", action="store_true", help="don't log each request")
    parser.add_argument("--capacity", type=int, default=0,
                        help="max requests processed at once; the rest wait (0 = unlimited)")
    parser.add_argument("--allow-control", action="store_true",
                        help="enable POST /control and /fault to change the delay or inject a fault at runtime")
    parser.add_argument("--control-token", default=os.environ.get("ADMIN_TOKEN", ""),
                        help="require this X-Admin-Token on control requests (default: $ADMIN_TOKEN)")
    parser.add_argument("--seed", type=int, help="seed for the jitter, for repeatable experiments")
    parser.add_argument("--heartbeat", metavar="HOST:PORT[=ADVERTISE]", action="append", default=[],
                        help="send UDP heartbeats (CPU, memory, active connections) to an LB here; repeatable. "
                             "=ADVERTISE: the address that LB knows this backend by (default: --advertise)")
    parser.add_argument("--advertise", metavar="HOST:PORT",
                        help="address the LB knows this backend by (default: 127.0.0.1:<port>)")
    args = parser.parse_args()

    name = args.name or f"backend-{args.port}"
    server = BackendServer((args.host, args.port), name, args.delay_ms, args.jitter_ms, args.quiet,
                           capacity=args.capacity, allow_control=args.allow_control, seed=args.seed,
                           control_token=args.control_token)
    print(f"{name} listening on {args.host}:{args.port} (delay {args.delay_ms}ms + jitter {args.jitter_ms}ms"
          f"{f', capacity {args.capacity}' if args.capacity else ''})")
    agent = None
    if args.heartbeat:
        # Imported here so a backend without --heartbeat doesn't need psutil.
        from backend.agent import HeartbeatAgent, parse_target
        default_advertise = args.advertise or f"127.0.0.1:{args.port}"
        targets = [parse_target(text, default_advertise) for text in args.heartbeat]
        agent = HeartbeatAgent(targets, server.in_flight)
        agent.start()
        for (lb_host, lb_port), advertise in targets:
            print(f"{name} sending heartbeats to {lb_host}:{lb_port} as {advertise}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if agent:
            agent.stop()
        server.server_close()


if __name__ == "__main__":
    main()
