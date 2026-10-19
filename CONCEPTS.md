# Concepts map

Every feature in this project, the file that implements it, and the CS concept it demonstrates.

| Feature | File | Subject | Concept | Interview one-liner |
|---------|------|---------|---------|---------------------|
| Normalised schema | `db/schema.sql` | DBMS | Third normal form (3NF) | Every non-key column depends only on its table's key, so facts like a server's address are stored once and can't drift out of sync. |
| Foreign keys + `ON DELETE` rules | `db/schema.sql` | DBMS | Referential integrity | FKs stop orphan rows; servers use RESTRICT + soft delete to keep history, run results CASCADE with their run. |
| CHECK / NOT NULL constraints | `db/schema.sql` | DBMS | Domain integrity | The database itself rejects impossible data (CPU 150%, check_type 'udp'), not just the application. |
| Atomic schema migration | `scripts/init_db.py` | DBMS | Transactions (transactional DDL) | Postgres runs CREATE TABLE inside a transaction, so the schema is applied entirely or not at all. |
| Stored run aggregates | `db/schema.sql` (`run_results`) | DBMS / System design | Controlled denormalisation | p95 etc. are derivable from `requests` but cached so plots are cheap and results outlive the 7-day retention job. |
| Listening socket | `lb/server.py` | CN | socket → bind → listen → accept | The kernel completes the TCP 3-way handshake and queues up to `backlog` connections until `accept()` hands us one. |
| `SO_REUSEADDR` | `lb/server.py` | CN / OS | TIME_WAIT | Lets the LB restart immediately; otherwise old connections in TIME_WAIT make `bind()` fail with "address already in use". |
| Thread-per-connection | `lb/server.py` | OS | Threads, concurrency | Each accepted socket gets its own thread so one slow backend doesn't block other clients; Phase 2 replaces it with a bounded pool. |
| Surviving fd / thread exhaustion | `lb/server.py` | OS | File descriptor limits (`ulimit -n`), EMFILE | Each connection costs 2 fds; when `accept()` hits "Too many open files" we log, back off and continue, so one client fails instead of the whole LB crashing (measured: 100 requests at a 64-fd limit went from 100 failed + crash to 70 OK + LB up). |
| Buffered stream reader | `lb/http_parser.py` (`SocketReader`) | CN | TCP is a byte stream | `recv()` can return half a message or two messages, so we buffer and frame by `\r\n\r\n` + `Content-Length`. |
| Hand-written HTTP/1.1 parser | `lb/http_parser.py` | CN | Application-layer protocol framing | Request line, headers and body are parsed by hand; HEAD/204/304 responses have no body even if Content-Length says otherwise. |
| Strict parsing | `lb/http_parser.py` | CN / Security | HTTP request smuggling | We reject conflicting Content-Length, TE+CL, folded headers and spaces before the colon, because front and back servers could disagree on where a request ends. |
| Input validation with `fullmatch` | `lb/http_parser.py` | Security | Injection / bare-LF tricks | `re.match(r"^[0-9]+$")` accepts `"5\n"` because `$` matches before a trailing newline; `fullmatch` doesn't. Paths must be visible ASCII, so no log-injection escape codes or NUL bytes. |
| Size limits + timeouts | `lb/config.py`, `lb/proxy.py` | OS / Security | Resource exhaustion | Header (431) and body (413) caps plus socket timeouts stop a client from pinning memory or a thread forever. |
| `Expect: 100-continue` | `lb/proxy.py` | CN | Protocol handshake | The client asks before sending a large body; we answer `100 Continue` and only then read it. |
| Hop-by-hop header stripping | `lb/proxy.py` | CN | Proxy semantics (RFC 9110) | `Connection`, `Keep-Alive` etc. describe one TCP link, so a proxy drops them instead of forwarding them. |
| `X-Forwarded-For` | `lb/proxy.py` | CN | Reverse proxy | The backend only sees the LB's IP, so we append the real client IP to the header chain. |
| Gateway error mapping | `lb/proxy.py` | CN / System design | 502 / 503 / 504 | Refused or invalid backend → 502, no backend → 503, backend too slow → 504, so failures are explicit, not hangs. |
| Strategy interface | `lb/strategies/base.py`, `lb/router.py` | System design | Strategy pattern | Proxy code only calls `choose()`, so new routing algorithms plug in without touching forwarding. |
| Round robin with a lock | `lb/strategies/round_robin.py` | OS | Race condition, mutual exclusion | Counter read-increment-write is made atomic with a `Lock`; a test with 8 threads checks the split is exactly even. |
| Test backend | `backend/app.py` | System design | Simulated latency | `sleep()` models I/O-bound latency per backend; `/health` stays instant because it measures liveness, not speed. |
