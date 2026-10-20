Phase 1 is done. Now do Phase 2 only: concurrency. Read PROJECT_SPEC.md for context.

1. Replace thread-per-connection with a producer-consumer design:
   - The listener thread (producer) accepts connections and puts them into a bounded queue that YOU implement with a lock and condition variables (do not use queue.Queue; I need to show I understand the mechanism).
   - A fixed pool of N worker threads (consumers) takes connections from the queue and handles them.
   - When the queue is full, the listener waits (backpressure). Make queue size and pool size configurable.
   Explain first why a bounded buffer is needed and what would go wrong without the condition variables.

2. Shared server statistics (lb/stats.py):
   - Per-backend active connections, total requests and a recent-latency average.
   - Protect them with a readers-writers lock that you implement (many workers read stats on every routing decision; updates are less frequent). Explain which variant you chose (reader-preference or writer-preference) and its starvation risk.
   - If any code path ever needs more than one lock, document a fixed lock order in a comment and in CONCEPTS.md, and explain how that prevents deadlock.

3. Graceful shutdown: on SIGINT/SIGTERM stop accepting new connections, let workers finish in-flight requests (with a maximum wait time), then exit cleanly.

4. Measurement: write a small benchmark that sends concurrent requests and compares thread-per-connection vs the thread pool (throughput, average latency, and peak thread count). Save the results to a file. Use real measured numbers only.

Tests: bounded queue tests (blocks when full, wakes waiting consumers, no lost or duplicated items under many threads), a readers-writers lock test, a shutdown test.

Done when: tests pass and the benchmark results are saved. Update CONCEPTS.md (producer-consumer, condition variables, readers-writers, deadlock prevention, thread pool vs thread-per-connection, signals). Commit, then stop and summarise, including the benchmark numbers.
