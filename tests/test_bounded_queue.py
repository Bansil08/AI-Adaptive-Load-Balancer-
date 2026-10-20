"""Tests for lb.bounded_queue: blocking, wakeups, and no lost/duplicated items."""

import threading
import time

import pytest

from lb.bounded_queue import BoundedQueue, QueueClosed

# How long we wait to conclude "this thread is blocked". Long enough that a
# thread that wasn't blocked would certainly have finished.
BLOCKED_FOR = 0.2


def run_in_thread(fn) -> tuple[threading.Thread, threading.Event, list]:
    """Start fn in a thread; returns (thread, done event, [result or exception])."""
    done, result = threading.Event(), []

    def target():
        try:
            result.append(fn())
        except Exception as exc:
            result.append(exc)
        done.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, done, result


def test_fifo_order():
    q = BoundedQueue(3)
    for item in "abc":
        q.put(item)
    assert [q.get() for _ in range(3)] == ["a", "b", "c"]


def test_put_blocks_when_full_until_a_get_makes_room():
    q = BoundedQueue(2)
    q.put(1)
    q.put(2)
    _, done, result = run_in_thread(lambda: q.put(3))
    assert not done.wait(BLOCKED_FOR), "put() into a full queue should block"
    assert q.get() == 1  # frees a slot and notifies not_full
    assert done.wait(2), "the blocked producer was never woken"
    assert result == [True]
    assert [q.get(), q.get()] == [2, 3]


def test_put_timeout_returns_false_when_still_full():
    q = BoundedQueue(1)
    q.put("x")
    started = time.monotonic()
    assert q.put("y", timeout=0.1) is False
    assert time.monotonic() - started >= 0.1
    assert len(q) == 1


def test_get_blocks_until_a_put_wakes_the_consumer():
    q = BoundedQueue(2)
    _, done, result = run_in_thread(q.get)
    assert not done.wait(BLOCKED_FOR), "get() on an empty queue should block"
    q.put("item")
    assert done.wait(2), "the waiting consumer was never woken"
    assert result == ["item"]


def test_get_timeout_raises():
    with pytest.raises(TimeoutError):
        BoundedQueue(1).get(timeout=0.05)


def test_each_put_wakes_a_waiting_consumer():
    """Several consumers asleep at once: each put must wake one of them, none left stuck."""
    q = BoundedQueue(1)
    waiters = [run_in_thread(q.get) for _ in range(4)]
    time.sleep(BLOCKED_FOR)  # let all four reach wait()
    for i in range(4):
        # Capacity 1: each put only fits if the previous item's consumer was woken.
        assert q.put(i, timeout=2), "a put() never woke a waiting consumer"
    for _, done, _ in waiters:
        assert done.wait(2)
    assert sorted(r[0] for _, _, r in waiters) == [0, 1, 2, 3]


def test_no_lost_or_duplicated_items_under_many_threads():
    """8 producers and 8 consumers through a 4-slot queue. A race (e.g. checking
    the count without the lock, or `if` instead of `while` around wait()) shows
    up as a missing item, a duplicate, or an IndexError from popping an empty deque."""
    q = BoundedQueue(4)
    producers, consumers, per_producer = 8, 8, 2000
    received: list[list[int]] = [[] for _ in range(consumers)]
    errors = []

    def produce(p):
        for i in range(per_producer):
            q.put(p * per_producer + i)

    def consume(c):
        try:
            while True:
                received[c].append(q.get())
        except QueueClosed:
            pass
        except Exception as exc:  # pragma: no cover - only on a bug
            errors.append(exc)

    # Daemon threads + one shared deadline: if a bug leaves threads stuck in
    # wait() (e.g. a missing notify), the test fails in seconds instead of hanging.
    consumer_threads = [threading.Thread(target=consume, args=(c,), daemon=True) for c in range(consumers)]
    producer_threads = [threading.Thread(target=produce, args=(p,), daemon=True) for p in range(producers)]
    for t in consumer_threads + producer_threads:
        t.start()
    deadline = time.monotonic() + 10
    for t in producer_threads:
        t.join(max(0, deadline - time.monotonic()))
    assert not any(t.is_alive() for t in producer_threads), "producers stuck: a wakeup was lost"
    q.close()  # consumers drain the rest, then see QueueClosed
    for t in consumer_threads:
        t.join(max(0, deadline - time.monotonic()))
    assert not any(t.is_alive() for t in consumer_threads), "consumers stuck: a wakeup was lost"

    assert not errors
    everything = [item for items in received for item in items]
    assert len(everything) == producers * per_producer        # nothing lost
    assert sorted(everything) == list(range(producers * per_producer))  # nothing duplicated
    # Each producer's items arrive in the order it put them (FIFO per producer).
    for items in received:
        for p in range(producers):
            mine = [x for x in items if x // per_producer == p]
            assert mine == sorted(mine)


def test_close_wakes_blocked_consumers_and_producers():
    empty = BoundedQueue(1)
    _, consumer_done, consumer_result = run_in_thread(empty.get)
    full = BoundedQueue(1)
    full.put("x")
    _, producer_done, producer_result = run_in_thread(lambda: full.put("y"))
    time.sleep(BLOCKED_FOR)

    empty.close()
    full.close()
    assert consumer_done.wait(2) and isinstance(consumer_result[0], QueueClosed)
    assert producer_done.wait(2) and isinstance(producer_result[0], QueueClosed)


def test_items_queued_before_close_are_still_delivered():
    q = BoundedQueue(3)
    q.put("a")
    q.put("b")
    q.close()
    with pytest.raises(QueueClosed):
        q.put("c")
    assert [q.get(), q.get()] == ["a", "b"]
    with pytest.raises(QueueClosed):
        q.get()


def test_drain_empties_the_queue_and_frees_producers():
    q = BoundedQueue(2)
    q.put(1)
    q.put(2)
    _, done, _ = run_in_thread(lambda: q.put(3))
    time.sleep(BLOCKED_FOR)
    assert q.drain() == [1, 2]
    assert done.wait(2)
    assert q.drain() == [3]


@pytest.mark.parametrize("capacity", [0, -1])
def test_capacity_must_be_positive(capacity):
    with pytest.raises(ValueError):
        BoundedQueue(capacity)
