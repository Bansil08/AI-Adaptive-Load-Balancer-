"""A writer-preference readers-writers lock built from one condition variable.

Many readers may hold the lock at the same time (reading doesn't change
anything), but a writer needs it alone: no readers and no other writer.

Variant: WRITER preference. As soon as a writer is waiting, new readers queue
behind it instead of joining the readers already inside. Every routing decision
reads the stats, so reads arrive almost continuously; with READER preference
the overlapping readers could keep the count above zero forever and a writer
would never get in (writer starvation), leaving the stats stale.

The starvation risk moves to the other side: a steady stream of writers can
keep readers waiting. Here writes are short (a few integer updates), so each
writer leaves quickly and the waiting readers then all enter together.

Not reentrant: a thread holding the read lock that asks for the write lock
(or for the read lock again while a writer waits) deadlocks with itself.
"""

import threading
from contextlib import contextmanager
from typing import Iterator


class ReadWriteLock:
    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0            # threads currently reading
        self._writer = False         # a thread is currently writing
        self._waiting_writers = 0    # writers queued; blocks new readers (writer preference)

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                # The last reader out lets a waiting writer in.
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            # Wake everyone: either the next writer or all the queued readers.
            self._cond.notify_all()

    @contextmanager
    def read_locked(self) -> Iterator[None]:
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write_locked(self) -> Iterator[None]:
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()
