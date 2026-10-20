"""Tests for lb.rwlock: shared readers, exclusive writers, writer preference."""

import threading
import time

from lb.rwlock import ReadWriteLock

BLOCKED_FOR = 0.2


def start(fn) -> tuple[threading.Thread, threading.Event]:
    done = threading.Event()

    def target():
        fn()
        done.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, done


def test_many_readers_hold_the_lock_at_the_same_time():
    lock = ReadWriteLock()
    # The barrier only opens once all 5 readers are inside together; if reads
    # were exclusive, wait() would time out with BrokenBarrierError.
    barrier = threading.Barrier(5, timeout=2)
    errors = []

    def reader():
        with lock.read_locked():
            try:
                barrier.wait()
            except threading.BrokenBarrierError as exc:
                errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors


def test_writer_excludes_readers_and_other_writers():
    lock = ReadWriteLock()
    lock.acquire_write()
    # Each thread acquires and never releases, so whichever gets in keeps it.
    _, reader_done = start(lock.acquire_read)
    _, writer_done = start(lock.acquire_write)
    assert not reader_done.wait(BLOCKED_FOR), "reader got in while a writer held the lock"
    assert not writer_done.is_set(), "second writer got in while a writer held the lock"
    lock.release_write()
    # Exactly one of them can now proceed; the other is blocked by it.
    time.sleep(BLOCKED_FOR)
    assert reader_done.is_set() != writer_done.is_set()


def test_writer_waits_for_active_readers():
    lock = ReadWriteLock()
    lock.acquire_read()
    _, writer_done = start(lock.acquire_write)
    assert not writer_done.wait(BLOCKED_FOR), "writer got in while a reader held the lock"
    lock.release_read()
    assert writer_done.wait(2)


def test_writer_preference_new_readers_queue_behind_a_waiting_writer():
    """Reader A is inside, writer W waits for A, then reader B arrives. With
    writer preference B must NOT join A (that is how readers starve writers);
    W goes first once A leaves, then B."""
    lock = ReadWriteLock()
    order = []
    lock.acquire_read()  # reader A

    def writer():
        with lock.write_locked():
            order.append("writer")

    def late_reader():
        with lock.read_locked():
            order.append("reader B")

    _, writer_done = start(writer)
    time.sleep(BLOCKED_FOR)  # W is now waiting
    _, reader_done = start(late_reader)
    assert not reader_done.wait(BLOCKED_FOR), "new reader overtook the waiting writer"

    lock.release_read()  # reader A leaves
    assert writer_done.wait(2) and reader_done.wait(2)
    assert order == ["writer", "reader B"]


def test_readers_never_see_a_half_finished_write():
    """Writers update two values that must stay equal; readers check them.
    Without the lock a reader can run between the two assignments."""
    lock = ReadWriteLock()
    state = {"a": 0, "b": 0}
    torn = []
    stop = threading.Event()

    def writer():
        for _ in range(2000):
            with lock.write_locked():
                state["a"] += 1
                time.sleep(0)  # yield mid-update to invite a race
                state["b"] += 1

    def reader():
        while not stop.is_set():
            with lock.read_locked():
                if state["a"] != state["b"]:
                    torn.append((state["a"], state["b"]))

    # Daemon threads + one deadline, so a broken lock fails fast instead of hanging.
    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    writers = [threading.Thread(target=writer, daemon=True) for _ in range(4)]
    for t in readers + writers:
        t.start()
    deadline = time.monotonic() + 20
    for t in writers:
        t.join(max(0, deadline - time.monotonic()))
    stop.set()
    assert not any(t.is_alive() for t in writers), "writers starved by continuous readers"
    for t in readers:
        t.join(5)
    assert not torn
    assert state == {"a": 8000, "b": 8000}
