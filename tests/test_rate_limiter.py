"""Token bucket: bursts, empty buckets, refill over time, per-IP isolation, thread safety."""

import threading

import pytest

from lb.config import RateLimitSettings
from lb.rate_limiter import RateLimiter, TokenBucket
from tests.test_circuit_breaker import FakeClock


@pytest.fixture
def clock():
    return FakeClock()


def limiter(clock, rate=10.0, burst=5.0, **kwargs):
    return RateLimiter(RateLimitSettings(rate=rate, burst=burst, **kwargs), clock)


def test_allows_a_burst_then_rejects_when_empty(clock):
    rl = limiter(clock)
    assert [rl.allow("1.2.3.4") for _ in range(6)] == [True] * 5 + [False]


def test_refills_over_time(clock):
    # Steps are powers of two so the float arithmetic is exact.
    rl = limiter(clock, rate=4.0, burst=5.0)
    for _ in range(5):
        rl.allow("ip")
    assert not rl.allow("ip")
    clock.now += 0.125           # half a token
    assert not rl.allow("ip")
    clock.now += 0.125           # now a whole one (0.25 s * 4 tokens/s)
    assert rl.allow("ip")
    assert not rl.allow("ip")
    clock.now += 0.75            # three more
    assert [rl.allow("ip") for _ in range(4)] == [True, True, True, False]


def test_refill_never_exceeds_the_bucket_size(clock):
    rl = limiter(clock, rate=10.0, burst=5.0)
    rl.allow("ip")
    clock.now += 3600            # an hour idle
    assert [rl.allow("ip") for _ in range(6)] == [True] * 5 + [False]


def test_each_ip_has_its_own_bucket(clock):
    rl = limiter(clock, burst=2.0)
    assert [rl.allow("a") for _ in range(3)] == [True, True, False]
    assert rl.allow("b")


def test_retry_after_says_when_a_token_is_back(clock):
    rl = limiter(clock, rate=0.5, burst=1.0)  # one token every 2 s
    rl.allow("ip")
    assert rl.retry_after("ip") == 2
    clock.now += 1.5
    assert rl.retry_after("ip") == 1  # rounded up to whole seconds


def test_disabled_limiter_allows_everything(clock):
    rl = limiter(clock, burst=1.0, enabled=False)
    assert all(rl.allow("ip") for _ in range(100))


def test_idle_full_buckets_are_evicted_when_tracking_too_many_ips(clock):
    rl = limiter(clock, burst=2.0, max_clients=3)
    for ip in ("a", "b", "c"):
        rl.allow(ip)
    clock.now += 10               # all three have refilled: indistinguishable from new
    rl.allow("d")
    assert set(rl._buckets) == {"d"}


def test_concurrent_requests_never_spend_more_tokens_than_exist(clock):
    """With the clock frozen there are exactly `burst` tokens; 16 threads racing
    for them must get exactly that many, never more (a lost update would)."""
    rl = limiter(clock, rate=1.0, burst=500.0)
    granted = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        mine = sum(rl.allow("same-ip") for _ in range(100))
        with lock:
            granted.append(mine)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 500


def test_token_bucket_on_its_own():
    bucket = TokenBucket(rate=2.0, capacity=2.0, now=0.0)
    assert bucket.try_take(0.0) and bucket.try_take(0.0) and not bucket.try_take(0.0)
    assert bucket.seconds_until_token(0.0) == pytest.approx(0.5)
    assert bucket.try_take(0.5)
