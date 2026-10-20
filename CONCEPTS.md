# Concepts map

Every feature in this project, the file that implements it, and the CS concept it demonstrates.

| Feature | File | Subject | Concept | Interview one-liner |
|---------|------|---------|---------|---------------------|
| Normalised schema | `db/schema.sql` | DBMS | Third normal form (3NF) | Every non-key column depends only on its table's key, so facts like a server's address are stored once and can't drift out of sync. |
| Foreign keys + `ON DELETE` rules | `db/schema.sql` | DBMS | Referential integrity | FKs stop orphan rows; servers use RESTRICT + soft delete to keep history, run results CASCADE with their run. |
| CHECK / NOT NULL constraints | `db/schema.sql` | DBMS | Domain integrity | The database itself rejects impossible data (CPU 150%, check_type 'udp'), not just the application. |
| Atomic schema migration | `scripts/init_db.py` | DBMS | Transactions (transactional DDL) | Postgres runs CREATE TABLE inside a transaction, so the schema is applied entirely or not at all. |
| Stored run aggregates | `db/schema.sql` (`run_results`) | DBMS / System design | Controlled denormalisation | p95 etc. are derivable from `requests` but cached so plots are cheap and results outlive the 7-day retention job. |
| Sockets API | `lb/server.py`, `lb/proxy.py` | CN / OS | socket, bind, listen, accept, connect, send, recv, shutdown, close | Server: socket→bind→listen→accept gives one new socket per client; client side: `create_connection` does socket+connect (TCP 3-way handshake). Every socket is a file descriptor. |
| Listening socket | `lb/server.py` | CN | socket → bind → listen → accept | The kernel completes the TCP 3-way handshake and queues up to `backlog` connections until `accept()` hands us one. |
| `SO_REUSEADDR` | `lb/server.py` | CN / OS | TIME_WAIT | Lets the LB restart immediately; otherwise old connections in TIME_WAIT make `bind()` fail with "address already in use". |
| Thread-per-connection (baseline) | `lb/server.py` (`--thread-per-connection`) | OS | Threads, concurrency | Each accepted socket gets its own thread so one slow backend doesn't block other clients; simple, but the thread count is unbounded. Kept as the benchmark baseline. |
| Producer-consumer | `lb/server.py`, `lb/bounded_queue.py` | OS | Bounded buffer problem | The listener (producer) puts accepted sockets into a bounded queue; a fixed pool of workers (consumers) takes them out, so accepting and serving are decoupled. |
| Hand-written bounded queue | `lb/bounded_queue.py` | OS | Mutex + condition variables | One lock, two conditions (`not_full`, `not_empty`); `wait()` releases the lock and sleeps atomically, so no busy-waiting and no lost wakeups. Re-check in a `while` loop because another thread may take the slot first (and wakeups can be spurious). |
| Backpressure | `lb/server.py` (`_enqueue`) | OS / System design | Flow control | When the queue is full the listener stops calling `accept()`; new connections wait in the kernel's listen backlog, and beyond that the kernel drops SYNs, so overload costs latency, not unbounded memory or threads. |
| Fixed thread pool | `lb/server.py` | OS | Thread pool vs thread-per-connection | N workers are created once; thread count (and stack memory) stays constant no matter how many clients connect. A worker catches unexpected errors so a bug can't permanently shrink the pool. |
| Pool vs thread-per-connection (measured) | `experiments/benchmark_concurrency.py`, `experiments/results/concurrency_benchmark.md` | OS / System design | Bounded vs unbounded concurrency | 50 ms backends, 200 clients: 32-worker pool 564 req/s (p95 377 ms, capped by 32 / 0.057 s), 256-worker pool 3426 req/s at 257 threads, thread-per-connection 3378 req/s at 201 threads. At 500 clients thread-per-connection was ~9% faster (4652 vs 4276 req/s) but used 437 threads vs a fixed 257; the pool's win is a hard cap on threads, not speed. |
| Ephemeral port exhaustion | `experiments/benchmark_concurrency.py` | CN / OS | TIME_WAIT, ephemeral ports | A new TCP connection per request leaves each closed one in TIME_WAIT for 2·MSL (30 s on macOS) holding one of ~16k ephemeral ports; at ~3.4k req/s the benchmark hit `EADDRNOTAVAIL` until it waited for TIME_WAIT to clear between runs. Connection pooling (Phase 3) is the real fix. |
| Whole-request deadline (slowloris) | `lb/http_parser.py` (`SocketReader`), `lb/proxy.py` | OS / Security | Resource exhaustion in a fixed pool | A socket timeout only limits the gap between two `recv()`s, so a client dripping one byte at a time held a worker forever (measured 18.7 s and counting at a 1 s timeout). Now the whole request must arrive within `client_timeout` (3 s). Known limit: each slow client still holds a worker for up to that long (+2 s lingering close), so ~32 slow clients can stall a 32-worker pool for a few seconds; the real fix is `select`/`epoll`, which this project deliberately leaves out. |
| Readers-writers lock | `lb/rwlock.py` | OS | Readers-writers problem | Many readers share the lock, a writer gets it alone; built from one condition variable and three counters. |
| Writer preference | `lb/rwlock.py` | OS | Starvation | A waiting writer blocks *new* readers; otherwise overlapping readers could keep it out forever (writer starvation). The trade-off: a steady stream of writers can starve readers instead. |
| Shared server stats | `lb/stats.py` | OS / System design | Shared state, consistent snapshots | Active connections, total requests and an EWMA latency per backend; readers get an immutable copy taken under the read lock, so all backends are seen at one instant. |
| Recent-latency average (EWMA) | `lb/stats.py` | System design | Exponentially weighted moving average | `avg = 0.2·sample + 0.8·avg` needs O(1) memory and forgets old samples, so it tracks a backend that slows down (a plain mean wouldn't). |
| Deadlock prevention | `lb/server.py` (docstring), below | OS | Lock ordering (breaks circular wait) | No code path holds two locks at once; if one ever must, it takes them in one documented global order, which makes a cycle of waiting threads impossible. |
| Graceful shutdown | `lb/server.py` (`_drain`) | OS / System design | Draining | On SIGINT/SIGTERM: close the listening socket (new connects refused), let workers finish in-flight and already-queued requests, give up after `shutdown_timeout`. |
| Signal handling | `lb/server.py` (`main`) | OS | Signals, async-signal safety | The handler only sets a flag: it runs on the main thread between bytecodes, so taking a lock (even logging's) could deadlock against the code it interrupted. A second signal gets the default action and kills immediately. |
| Surviving fd / thread exhaustion | `lb/server.py` | OS | File descriptor limits (`ulimit -n`), EMFILE | Each connection costs 2 fds; when `accept()` hits "Too many open files" we log, back off and continue, so one client fails instead of the whole LB crashing (measured: 100 requests at a 64-fd limit went from 100 failed + crash to 70 OK + LB up). |
| TCP byte stream | `lb/http_parser.py` (`SocketReader`) | CN | Stream vs message delivery | TCP guarantees bytes arrive in order, not that one send() equals one recv(); so we read until `\r\n\r\n`, then exactly Content-Length bytes, keeping leftovers buffered. |
| Buffered stream reader | `lb/http_parser.py` (`SocketReader`) | CN | TCP is a byte stream | `recv()` can return half a message or two messages, so we buffer and frame by `\r\n\r\n` + `Content-Length`. |
| HTTP message format | `lb/http_parser.py` | CN | Start line, headers, blank line, body | `GET /path HTTP/1.1` or `HTTP/1.1 200 OK`, then `Name: value` lines, a blank line (CRLF CRLF), then a body whose length Content-Length gives. |
| Hand-written HTTP/1.1 parser | `lb/http_parser.py` | CN | Application-layer protocol framing | Request line, headers and body are parsed by hand; HEAD/204/304 responses have no body even if Content-Length says otherwise. |
| Strict parsing | `lb/http_parser.py` | CN / Security | HTTP request smuggling | We reject conflicting Content-Length, TE+CL, folded headers and spaces before the colon, because front and back servers could disagree on where a request ends. |
| Input validation with `fullmatch` | `lb/http_parser.py` | Security | Injection / bare-LF tricks | `re.match(r"^[0-9]+$")` accepts `"5\n"` because `$` matches before a trailing newline; `fullmatch` doesn't. Paths must be visible ASCII, so no log-injection escape codes or NUL bytes. |
| Size limits + timeouts | `lb/config.py`, `lb/proxy.py` | OS / Security | Resource exhaustion | Header (431) and body (413) caps plus socket timeouts stop a client from pinning memory or a thread forever. |
| `Expect: 100-continue` | `lb/proxy.py` | CN | Protocol handshake | The client asks before sending a large body; we answer `100 Continue` and only then read it. |
| Hop-by-hop header stripping | `lb/proxy.py` | CN | Proxy semantics (RFC 9110) | `Connection`, `Keep-Alive` etc. describe one TCP link, so a proxy drops them instead of forwarding them. |
| Reverse proxy | `lb/proxy.py` | CN / System design | Reverse vs forward proxy | Clients talk to the LB as if it were the server; it picks a backend, opens its own TCP connection, relays the response, and hides backend addresses from clients. |
| `X-Forwarded-For` | `lb/proxy.py` | CN | Reverse proxy | The backend only sees the LB's IP, so we append the real client IP to the header chain. |
| 502 vs 504 | `lb/proxy.py`, `lb.toml` | CN | Gateway error semantics | 502 Bad Gateway = backend refused, reset, or sent garbage; 504 Gateway Timeout = backend didn't connect/answer within the configured timeout; 503 = no backend at all. |
| Gateway error mapping | `lb/proxy.py` | CN / System design | 502 / 503 / 504 | Refused or invalid backend → 502, no backend → 503, backend too slow → 504, so failures are explicit, not hangs. |
| Lingering close | `lb/proxy.py` (`close_gracefully`) | CN | FIN vs RST | `close()` with unread bytes in the receive buffer sends RST, which can destroy our response; `shutdown(SHUT_WR)` + drain + close lets the client read it (measured: a 413 was lost 10/10 times before). |
| Config file | `lb.toml`, `lb/config.py` | System design | Configuration vs code | Backends, port, strategy and timeouts live in TOML (stdlib `tomllib`), validated on load, so changing them needs no code change. |
| Backends in Docker Compose | `docker-compose.yml` | OS / System design | Containers, PID 1 signal handling | `init: true` adds a tiny init as PID 1 to forward SIGTERM; PID 1 ignores signals without handlers, so stop would otherwise wait 10 s for SIGKILL (measured 0.24 s with it). |
| Strategy interface | `lb/strategies/base.py`, `lb/router.py` | System design | Strategy pattern | Proxy code only calls `choose()`, so new routing algorithms plug in without touching forwarding. |
| Round robin with a lock | `lb/strategies/round_robin.py` | OS | Race condition, mutual exclusion | Counter read-increment-write is made atomic with a `Lock`; a test with 8 threads checks the split is exactly even. |
| Test backend | `backend/app.py` | System design | Simulated latency | `sleep()` models I/O-bound latency per backend; `/health` stays instant because it measures liveness, not speed. |

## Deadlock prevention: lock order

Locks in the load balancer:

| Lock | File | Protects |
|------|------|----------|
| `BoundedQueue._lock` (+ its two conditions) | `lb/bounded_queue.py` | the queue's deque and closed flag |
| `LoadBalancer._handlers_lock` | `lb/server.py` | set of live thread-per-connection handlers |
| `ServerStats._lock` (`ReadWriteLock`, internally one mutex) | `lb/stats.py` | per-backend counters |
| `RoundRobinStrategy._lock` | `lb/strategies/round_robin.py` | the round-robin counter |

A deadlock needs all four Coffman conditions: mutual exclusion, hold-and-wait, no preemption, and **circular wait** (thread 1 holds A and waits for B while thread 2 holds B and waits for A). We break circular wait:

1. Today no code path holds two of these locks at the same time (each critical section is a few lines and calls nothing that locks), so hold-and-wait never happens either.
2. If a later phase needs two at once, it must take them in the order of the table above (queue → handlers → stats → strategy) and release in reverse. With one global order every thread waits only "downwards", so no cycle can form.
3. Never log, block on I/O, or call user code while holding a lock: `logging` has its own internal lock, and a slow call while holding a lock stalls every other thread that needs it.
4. `ReadWriteLock` is not reentrant: upgrading read → write in the same thread deadlocks with itself, so code takes the write lock from the start when it might write.
