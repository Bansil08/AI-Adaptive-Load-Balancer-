"""Test backend HTTP server with configurable artificial delay and a /health endpoint.

Run:  python -m backend.app --port 9001 --name fast --delay-ms 20
      python -m backend.app --port 9001 --heartbeat 127.0.0.1:9100   # + UDP heartbeats to the LB
"""

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class BackendServer(ThreadingHTTPServer):
    """http.server with a thread per request, plus the delay settings."""

    daemon_threads = True
    # socketserver's default listen backlog is only 5: with more simultaneous
    # connects the kernel drops the extra SYNs and clients stall for a ~1 s
    # retransmit. Match the load balancer's backlog so the backend isn't the bottleneck.
    request_queue_size = 128

    def __init__(self, address, name: str, delay_ms: float = 0, jitter_ms: float = 0, quiet: bool = False):
        super().__init__(address, BackendHandler)
        self.name = name
        self.delay_ms = delay_ms
        self.jitter_ms = jitter_ms
        self.quiet = quiet
        # Requests being handled right now, reported in heartbeats. Each request
        # runs in its own thread, so the counter needs a lock (+= is not atomic).
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()

    def in_flight(self) -> int:
        with self._in_flight_lock:
            return self._in_flight

    def _add_in_flight(self, delta: int) -> None:
        with self._in_flight_lock:
            self._in_flight += delta


class BackendHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 so every response carries Content-Length (needed for keep-alive,
    # which Phase 3's connection pooling relies on).
    protocol_version = "HTTP/1.1"
    server: BackendServer

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle(send_body=False)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_GET

    def _handle(self, send_body: bool = True) -> None:
        self.server._add_in_flight(1)
        try:
            self._respond(send_body)
        finally:
            self.server._add_in_flight(-1)

    def _respond(self, send_body: bool) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(length) if length else b""

        if self.path == "/health":
            # No artificial delay: a health check asks "is the process alive?",
            # not "is it fast?".
            payload = {"status": "ok", "server": self.server.name}
        else:
            # sleep() simulates I/O-bound latency (e.g. waiting on a database).
            # CPU-bound slowness comes from Docker CPU limits in later phases.
            delay_ms = self.server.delay_ms + random.uniform(0, self.server.jitter_ms)
            time.sleep(delay_ms / 1000)
            payload = {
                "server": self.server.name,
                "method": self.command,
                "path": self.path,
                "delay_ms": round(delay_ms, 1),
                "body_bytes": len(request_body),
                "x_forwarded_for": self.headers.get("X-Forwarded-For"),
            }

        body = json.dumps(payload).encode() + b"\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Backend", self.server.name)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

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
    parser.add_argument("--heartbeat", metavar="HOST:PORT",
                        help="send UDP heartbeats (CPU, memory, active connections) to the LB here")
    parser.add_argument("--advertise", metavar="HOST:PORT",
                        help="address the LB knows this backend by (default: 127.0.0.1:<port>)")
    args = parser.parse_args()

    name = args.name or f"backend-{args.port}"
    server = BackendServer((args.host, args.port), name, args.delay_ms, args.jitter_ms, args.quiet)
    print(f"{name} listening on {args.host}:{args.port} (delay {args.delay_ms}ms + jitter {args.jitter_ms}ms)")
    agent = None
    if args.heartbeat:
        # Imported here so a backend without --heartbeat doesn't need psutil.
        from backend.agent import HeartbeatAgent
        lb_host, _, lb_port = args.heartbeat.rpartition(":")
        advertise = args.advertise or f"127.0.0.1:{args.port}"
        agent = HeartbeatAgent((lb_host, int(lb_port)), advertise, server.in_flight)
        agent.start()
        print(f"{name} sending heartbeats to {args.heartbeat} as {advertise}")
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
