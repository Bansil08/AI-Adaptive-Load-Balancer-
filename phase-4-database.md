Phase 3 is done. Now do Phase 4 only. Read PROJECT_SPEC.md for the schema.

1. Request logging (lb/db_writer.py): worker threads must never write to the database directly. They push a log record (server, method, path, status, response time, retried flag, run id) onto an in-memory queue. One DB writer thread takes records in batches (flush when the batch reaches N records or T milliseconds pass, whichever comes first) and inserts each batch in a single transaction. On shutdown, flush everything that remains. Explain why a single writer with batching is better than every worker inserting rows itself.

2. Also persist health check results and the UDP heartbeat metrics into health_checks and server_metrics (batched the same way).

3. Measure batch vs single-row inserts (rows per second) and record the real numbers.

4. Indexes: first run the dashboard queries on a large dataset (generate at least 1 million requests rows with a seeding script) and capture EXPLAIN ANALYZE output. Then add the indexes from the spec, run them again, and record both plans and timings in CONCEPTS.md with a plain-language explanation of what changed.

5. Queries in db/queries.sql:
   - p95 and average latency per server per minute for the last 15 minutes
   - error rate per server
   - request count per server
   - training-data export joining requests with the most recent server_metrics row before each request
   Create views for the dashboard queries.

6. Retention: a job that deletes rows older than 7 days in small batches (explain why one huge DELETE is a bad idea).

Tests: batching behaviour (size and time triggers), flush on shutdown, and that a failed batch is rolled back without losing the writer thread.

Done when: tests pass, the index before/after results are recorded, and queries return correct results. Update CONCEPTS.md (transactions and ACID, batching, indexing with EXPLAIN, views, joins, retention). Commit, then stop and summarise.
