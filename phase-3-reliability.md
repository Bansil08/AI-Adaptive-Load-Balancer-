Phase 2 is done. Now do Phase 3 only. Read PROJECT_SPEC.md for context. Implement these one at a time, briefly explaining each before coding it.

1. Health checks (lb/health.py): a background thread checks every backend every few seconds, at two levels: TCP (can I connect?) and HTTP (does GET /health return 200 within a timeout?). A backend is marked down after K consecutive failures and up after M consecutive successes. The router must only pick healthy backends.

2. Circuit breaker (per backend): states CLOSED, OPEN and HALF_OPEN, implemented with the state pattern. Open after a threshold of real request failures, wait a cooldown, then allow a few trial requests in half-open. Explain how this differs from health checks.

3. UDP heartbeats: backend/agent.py sends CPU %, memory % and active connections over UDP every second (use psutil). lb/heartbeat.py receives them and updates lb/stats.py. Explain why UDP is acceptable here but TCP is used for requests.

4. Connection pooling to backends: reuse persistent TCP connections (HTTP/1.1 keep-alive) instead of opening a new one per request, with a per-backend pool size limit and handling of connections the backend closed. Benchmark with and without pooling and record real numbers. Explain the three-way handshake cost this avoids.

5. Timeouts and retries: retry only idempotent methods (GET, HEAD) on another backend, with exponential backoff and a maximum retry count. Never retry POST. Record whether a request was retried.

6. Rate limiting (lb/rate_limiter.py): token bucket per client IP, return 429 Too Many Requests when empty, thread-safe.

7. Admin API (lb/admin.py): a small separate port with endpoints to list backends, add a backend and remove one (removal drains its in-flight requests first).

8. Optional: sticky sessions by hashing the client IP, enabled by a config flag.

Tests for: health state transitions, all circuit breaker transitions, the token bucket (including refill over time), retry rules (GET retried, POST not), and connection pool reuse.

Demo check: kill one backend while sending traffic and show that errors stop within a few seconds and traffic moves to the others; restart it and show it rejoins.

Done when: tests pass and the demo works. Update CONCEPTS.md. Commit, then stop and summarise with the pooling benchmark numbers.
