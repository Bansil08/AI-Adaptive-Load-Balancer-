"""Pool of persistent (HTTP/1.1 keep-alive) TCP connections to each backend.

Without a pool, every proxied request pays for a new TCP connection: the
3-way handshake (SYN, SYN-ACK, ACK) costs one round trip before the request
can even be sent, the kernel allocates a socket and file descriptor, and
after close() the connection sits in TIME_WAIT for 2*MSL holding one of the
~16k ephemeral ports (the Phase 2 benchmark ran out of them). The backend
also pays: http.server starts a new thread for every connection. With
keep-alive, one connection carries many requests one after another.

Sizing: `size` is the number of IDLE connections kept per backend. When all
are busy, an extra connection is opened and simply closed after use instead
of being kept, so a burst is never blocked waiting for the pool. The total
number of connections per backend is already bounded by the worker count.

Connections the backend closed: a backend may close an idle connection at any
time (restart, its own idle timeout). Before reusing one we peek at it without
blocking: if recv() reports end-of-file (b"") or an error, the backend has
closed it, so we throw it away and try the next. Connections idle longer than
`idle_timeout` are dropped too, since the backend has probably closed them.
The check can still race with a close that happens right after it; if a
reused connection then fails before any response byte arrives, the proxy
resends a GET/HEAD once on a fresh connection (lb/proxy.py, _StaleConnection).
"""

import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

from lb.config import PoolSettings
from lb.strategies.base import Backend


@dataclass
class PooledConnection:
    sock: socket.socket
    reused: bool          # True if it already carried at least one request
    idle_since: float = 0.0


@dataclass(frozen=True)
class PoolCounters:
    created: int       # new TCP connections opened (each one = one 3-way handshake)
    reused: int        # requests sent on an existing connection
    discarded: int     # idle connections found closed or expired and thrown away
    idle: int          # connections idle in the pool right now


def peer_closed(sock: socket.socket) -> bool:
    """True if the other side has closed this idle connection (or it is broken)."""
    try:
        sock.setblocking(False)
        try:
            # MSG_PEEK leaves any data in the buffer. An idle HTTP connection
            # should have nothing to read: b"" means the peer sent FIN, and
            # unexpected bytes mean we can't trust where the next response starts.
            sock.recv(1, socket.MSG_PEEK)
        finally:
            sock.setblocking(True)
    except BlockingIOError:
        return False  # nothing to read and no FIN: still open
    except OSError:
        return True   # e.g. connection reset by the peer
    return True


class ConnectionPool:
    def __init__(self, settings: PoolSettings, connect_timeout: float = 2.0):
        self.settings = settings
        self.connect_timeout = connect_timeout
        self._lock = threading.Lock()
        # Per backend: a stack (LIFO) of idle connections. Most recently used
        # first: it is the one most likely still open, and the rarely used ones
        # at the bottom age out via idle_timeout.
        self._idle: dict[Backend, deque[PooledConnection]] = {}
        self._created = self._reused = self._discarded = 0

    @property
    def enabled(self) -> bool:
        return self.settings.size > 0

    def acquire(self, backend: Backend) -> PooledConnection:
        """An open connection to `backend`: an idle pooled one if possible, else a new one."""
        while True:
            with self._lock:
                stack = self._idle.get(backend)
                conn = stack.pop() if stack else None
            if conn is None:
                break
            # Checked outside the lock: it is a system call, and no other
            # thread can see this connection now that it is off the stack.
            expired = time.monotonic() - conn.idle_since > self.settings.idle_timeout
            if expired or peer_closed(conn.sock):
                conn.sock.close()
                with self._lock:
                    self._discarded += 1
                continue
            with self._lock:
                self._reused += 1
            conn.reused = True
            return conn
        return self.connect(backend)

    def connect(self, backend: Backend) -> PooledConnection:
        """Always a brand-new connection (never one from the pool)."""
        # create_connection resolves the name and performs the TCP 3-way handshake.
        sock = socket.create_connection((backend.host, backend.port), timeout=self.connect_timeout)
        # Nagle off: on a long-lived connection, the small last segment of a
        # large request body would otherwise wait for the backend's (delayed) ACK.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with self._lock:
            self._created += 1
        return PooledConnection(sock, reused=False)

    def release(self, backend: Backend, conn: PooledConnection, reusable: bool) -> None:
        """Give a connection back after a complete response; closes it if it can't be kept."""
        if reusable and self.enabled:
            conn.idle_since = time.monotonic()
            with self._lock:
                stack = self._idle.setdefault(backend, deque())
                if len(stack) < self.settings.size:
                    stack.append(conn)
                    return
        conn.sock.close()

    def discard_backend(self, backend: Backend) -> None:
        """Close all idle connections to a backend (it was removed)."""
        with self._lock:
            stack = self._idle.pop(backend, deque())
        for conn in stack:
            conn.sock.close()

    def close_all(self) -> None:
        with self._lock:
            stacks, self._idle = list(self._idle.values()), {}
        for stack in stacks:
            for conn in stack:
                conn.sock.close()

    def counters(self) -> PoolCounters:
        with self._lock:
            idle = sum(len(s) for s in self._idle.values())
            return PoolCounters(self._created, self._reused, self._discarded, idle)
