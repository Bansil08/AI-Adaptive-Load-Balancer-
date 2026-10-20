"""A bounded FIFO queue built from one lock and two condition variables.

This is the classic producer-consumer (bounded buffer) problem. The listener
thread produces accepted connections; worker threads consume them.

Why bounded: if the listener could enqueue without limit, a traffic spike
would grow the queue until memory runs out, and clients at the back would
wait so long their own timeouts fire. With a capacity, a full queue makes the
listener wait (backpressure); new connections then wait in the kernel's
listen backlog instead of in our memory.

Why condition variables: without them a consumer facing an empty queue can
only busy-wait (lock, check, unlock, repeat), burning CPU. Checking without
the lock is worse: an item can arrive between the check and going to sleep,
and the wakeup is lost. Condition.wait() releases the lock and sleeps as one
atomic step, then re-acquires it before returning.

queue.Queue does the same internally; it is written by hand here on purpose.
"""

import threading
import time
from collections import deque
from typing import Generic, TypeVar

T = TypeVar("T")


class QueueClosed(Exception):
    """put() after close(), or get() on a closed queue that has been drained."""


class BoundedQueue(Generic[T]):
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._items: deque[T] = deque()
        self._closed = False
        # Both conditions share ONE lock, so "check the count, then wait" is a
        # single critical section for producers and consumers alike.
        self._lock = threading.Lock()
        self._not_full = threading.Condition(self._lock)   # producers wait here
        self._not_empty = threading.Condition(self._lock)  # consumers wait here

    def put(self, item: T, timeout: float | None = None) -> bool:
        """Add an item, waiting while the queue is full.

        Returns False if `timeout` seconds pass with the queue still full.
        Raises QueueClosed if the queue is (or becomes) closed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            # `while`, not `if`: after waking, another producer may have filled
            # the slot first, and wait() can also wake spuriously. Always re-check.
            while len(self._items) >= self.capacity and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._not_full.wait(remaining)
            if self._closed:
                raise QueueClosed
            self._items.append(item)
            # Wake exactly one waiting consumer: one new item can only feed one.
            self._not_empty.notify()
            return True

    def get(self, timeout: float | None = None) -> T:
        """Remove and return the oldest item, waiting while the queue is empty.

        Raises TimeoutError if `timeout` passes with nothing to take, and
        QueueClosed once the queue is closed AND empty (items queued before
        close() are still handed out, so a shutdown can drain them).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while not self._items and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                self._not_empty.wait(remaining)
            if not self._items:
                raise QueueClosed
            item = self._items.popleft()
            self._not_full.notify()
            return item

    def close(self) -> None:
        """Refuse further put()s and wake every waiting thread so it can see that."""
        with self._lock:
            self._closed = True
            self._not_full.notify_all()
            self._not_empty.notify_all()

    def drain(self) -> list[T]:
        """Remove and return everything still queued (used when a shutdown gives up)."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            self._not_full.notify_all()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
