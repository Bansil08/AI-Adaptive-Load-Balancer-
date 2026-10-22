"""Token-bucket rate limiter keyed by client IP.

Each client IP has a bucket holding up to `burst` tokens that refills at
`rate` tokens per second. A request takes one token; with none left it is
rejected with 429 Too Many Requests. So a client may send short bursts of up
to `burst` requests, but its long-run average is capped at `rate` per second.

Lazy refill: no timer thread adds tokens. Each check computes how many tokens
accumulated since the bucket was last touched: min(burst, tokens + elapsed * rate).

Thread safety: workers check buckets concurrently, and "read tokens, compare,
subtract" must be one atomic step or two threads could spend the same last
token. One lock covers the dict and every bucket; each critical section is a
few arithmetic operations, so per-bucket locks would add complexity for nothing.
"""

import math
import threading
import time
from typing import Callable

from lb.config import RateLimitSettings


class TokenBucket:
    """One bucket. Not thread-safe by itself: RateLimiter calls it under its lock."""

    def __init__(self, rate: float, capacity: float, now: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity  # a new client starts with a full bucket
        self.updated = now

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_take(self, now: float) -> bool:
        self._refill(now)
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def seconds_until_token(self, now: float) -> float:
        self._refill(now)
        return max(0.0, (1 - self.tokens) / self.rate)

    def is_full(self, now: float) -> bool:
        self._refill(now)
        return self.tokens >= self.capacity


class RateLimiter:
    def __init__(self, settings: RateLimitSettings = RateLimitSettings(),
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self._clock = clock  # injectable so tests can move time forward without sleeping
        self._lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}

    def allow(self, client_ip: str) -> bool:
        """Spend one token for this client. False means answer 429."""
        if not self.settings.enabled:
            return True
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(client_ip)
            if bucket is None:
                if len(self._buckets) >= self.settings.max_clients:
                    self._evict_full(now)
                bucket = self._buckets[client_ip] = TokenBucket(self.settings.rate, self.settings.burst, now)
            return bucket.try_take(now)

    def retry_after(self, client_ip: str) -> int:
        """Whole seconds until this client has a token again (for the Retry-After header)."""
        with self._lock:
            bucket = self._buckets.get(client_ip)
            wait = bucket.seconds_until_token(self._clock()) if bucket else 0.0
        return max(1, math.ceil(wait))

    def _evict_full(self, now: float) -> None:
        # A full bucket is indistinguishable from a brand-new one, so forgetting
        # it changes nothing for that client. This bounds memory when many
        # different IPs (or spoofed ones) show up.
        for ip in [ip for ip, b in self._buckets.items() if b.is_full(now)]:
            del self._buckets[ip]
