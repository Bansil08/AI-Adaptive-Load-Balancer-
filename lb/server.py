"""Listener: accepts TCP connections and hands each one to a handler thread.

Phase 1 uses thread-per-connection, the simplest concurrent design. Phase 2
replaces it with a bounded queue + fixed-size thread pool and measures the
difference against this version.

Run:  python -m lb.server --port 8080 --backend 127.0.0.1:9001 --backend 127.0.0.1:9002
"""

import argparse
import logging
import socket
import threading
import time

from lb import config
from lb.proxy import handle_client
from lb.router import Router
from lb.strategies import STRATEGIES, Backend

log = logging.getLogger(__name__)

# How often the accept loop wakes up to check whether it should stop.
ACCEPT_POLL_SECONDS = 0.5
# Pause after a failed accept() so we don't spin at 100% CPU while out of file descriptors.
ACCEPT_ERROR_BACKOFF_SECONDS = 0.1


class LoadBalancer:
    def __init__(self, router: Router, host: str = config.LB_HOST, port: int = config.LB_PORT):
        self.router = router
        self._address = (host, port)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def start(self) -> tuple[str, int]:
        """Create the listening socket. Returns the bound (host, port); port 0 picks a free one."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # IPv4 + TCP
        # Lets us restart immediately. Otherwise connections from the previous run
        # still in TIME_WAIT make bind() fail with "Address already in use".
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(self._address)
        sock.listen(config.LISTEN_BACKLOG)
        # accept() blocks; a timeout makes it return regularly so the loop can
        # notice the stop flag. (Closing a socket from another thread does not
        # reliably wake a blocked accept() on every OS.)
        sock.settimeout(ACCEPT_POLL_SECONDS)
        self._sock = sock
        return sock.getsockname()[:2]

    def serve_forever(self) -> None:
        if self._sock is None:
            self.start()
        try:
            while not self._stop.is_set():
                try:
                    client_sock, client_addr = self._sock.accept()
                except TimeoutError:
                    continue
                except OSError as exc:
                    # e.g. EMFILE "Too many open files" during a connection flood.
                    # Uncaught, this would crash the whole LB and drop every
                    # in-flight request. Instead only this one client suffers
                    # (macOS resets its connection; Linux leaves it queued), and
                    # we back off briefly so fds can free up before retrying.
                    log.error("accept() failed: %s; backing off", exc)
                    time.sleep(ACCEPT_ERROR_BACKOFF_SECONDS)
                    continue
                # One thread per connection. daemon=True so they don't keep the
                # process alive on exit; Phase 2 replaces this with a draining shutdown.
                try:
                    threading.Thread(
                        target=handle_client,
                        args=(client_sock, client_addr, self.router),
                        name=f"conn-{client_addr[0]}:{client_addr[1]}",
                        daemon=True,
                    ).start()
                except RuntimeError as exc:
                    # "can't start new thread": the OS refused another thread.
                    # Drop this one connection rather than the whole server.
                    log.error("cannot start handler thread: %s; dropping connection", exc)
                    client_sock.close()
        finally:
            self._sock.close()

    def shutdown(self) -> None:
        """Ask serve_forever() to stop (safe to call from another thread)."""
        self._stop.set()


def main() -> None:
    parser = argparse.ArgumentParser(description="Adaptive load balancer")
    parser.add_argument("--host", default=config.LB_HOST)
    parser.add_argument("--port", type=int, default=config.LB_PORT)
    parser.add_argument(
        "--backend", action="append", type=Backend.parse, dest="backends",
        help="backend as host:port; repeat for several (default: $LB_BACKENDS)",
    )
    parser.add_argument("--strategy", choices=sorted(STRATEGIES), default="round_robin")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(threadName)s: %(message)s"
    )
    backends = args.backends or [Backend.parse(b) for b in config.LB_BACKENDS.split(",")]
    router = Router(backends, STRATEGIES[args.strategy]())
    lb = LoadBalancer(router, args.host, args.port)
    host, port = lb.start()
    log.info("listening on %s:%d, strategy=%s, backends=%s",
             host, port, args.strategy, ", ".join(map(str, backends)))
    try:
        lb.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
