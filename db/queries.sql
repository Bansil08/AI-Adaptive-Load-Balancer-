-- Dashboard and ML training-data queries, as views (Phase 4).
--
-- A view is a stored, named SELECT: `SELECT * FROM dashboard_error_rate` runs
-- the query below at that moment (nothing is cached; that would be a
-- MATERIALIZED view). The dashboard and ml/export_data.py read these names, so
-- the SQL lives in one place and can change without touching Python.
--
-- Applied by scripts/init_db.py after schema.sql. CREATE OR REPLACE keeps it
-- idempotent.
--
-- Index-friendly shape: the per-server views start from `servers` (a handful
-- of rows) and join `requests` ON r.server_id = s.id AND r.received_at >= ...
-- With the (server_id, received_at) index the planner can then fetch each
-- server's last 15 minutes with one small index range scan instead of reading
-- every row (see CONCEPTS.md, "Indexes: before and after").

-- p95 and average latency per server per minute, last 15 minutes.
CREATE OR REPLACE VIEW dashboard_latency_per_minute AS
SELECT s.id                                   AS server_id,
       s.address || ':' || s.port             AS server,
       date_trunc('minute', r.received_at)    AS minute,
       count(*)                               AS requests,
       round(avg(r.response_time_ms)::numeric, 2) AS avg_latency_ms,
       -- percentile_cont interpolates between the two nearest values; it is an
       -- "ordered-set aggregate": it sorts each group's values to find the 95th.
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY r.response_time_ms))::numeric, 2)
                                              AS p95_latency_ms
FROM servers s
JOIN requests r
  ON r.server_id = s.id
 AND r.received_at >= now() - interval '15 minutes'
GROUP BY s.id, server, minute;

-- Error rate per server, last 15 minutes. An error is a 5xx answer (502 bad
-- gateway, 503 no backend, 504 timeout, or a 5xx from the backend itself).
-- Requests no backend served (server_id NULL: 503 no healthy backend, 429
-- rate limited) get their own row, so they are not silently left out.
CREATE OR REPLACE VIEW dashboard_error_rate AS
SELECT s.id                                   AS server_id,
       s.address || ':' || s.port             AS server,
       count(r.id)                            AS requests,
       count(r.id) FILTER (WHERE r.status_code >= 500) AS errors,
       round(count(r.id) FILTER (WHERE r.status_code >= 500)::numeric
             / nullif(count(r.id), 0), 4)     AS error_rate
FROM servers s
-- LEFT JOIN with the conditions in ON (not WHERE): a server with no recent
-- traffic still appears, with 0 requests, instead of vanishing.
LEFT JOIN requests r
  ON r.server_id = s.id
 AND r.received_at >= now() - interval '15 minutes'
GROUP BY s.id, server
UNION ALL
SELECT NULL, '(no backend)',
       count(*),
       count(*) FILTER (WHERE status_code >= 500),
       round(count(*) FILTER (WHERE status_code >= 500)::numeric / nullif(count(*), 0), 4)
FROM requests
WHERE server_id IS NULL
  AND received_at >= now() - interval '15 minutes';

-- Request count per server, last 15 minutes (and how many were retries).
CREATE OR REPLACE VIEW dashboard_requests_per_server AS
SELECT s.id                                   AS server_id,
       s.address || ':' || s.port             AS server,
       s.status,
       count(r.id)                            AS requests,
       count(r.id) FILTER (WHERE r.retried)   AS retried
FROM servers s
LEFT JOIN requests r
  ON r.server_id = s.id
 AND r.received_at >= now() - interval '15 minutes'
GROUP BY s.id, server, s.status;

-- Training data for the latency model (Phase 6): every request a backend
-- served, joined with the most recent heartbeat from that backend BEFORE the
-- request arrived (what the load balancer could have known when routing it).
--
-- LATERAL lets the subquery refer to the current request row `r`, so it runs
-- once per request: "this server's metrics at or before r.received_at, newest
-- first, take one". With the (server_id, recorded_at) index that is a single
-- index probe; without it, a scan of all metrics for every request.
-- LEFT JOIN ... ON true keeps requests that have no earlier heartbeat (NULL features).
CREATE OR REPLACE VIEW training_data AS
SELECT r.id                     AS request_id,
       r.run_id,
       r.server_id,
       r.received_at,
       r.method,
       r.path,
       r.status_code,
       r.retried,
       m.cpu_percent,
       m.memory_percent,
       m.active_connections,
       extract(epoch FROM r.received_at - m.recorded_at) * 1000 AS metrics_age_ms,
       r.response_time_ms        -- the target the model learns to predict
FROM requests r
LEFT JOIN LATERAL (
    SELECT cpu_percent, memory_percent, active_connections, recorded_at
    FROM server_metrics
    WHERE server_id = r.server_id
      AND recorded_at <= r.received_at
    ORDER BY recorded_at DESC
    LIMIT 1
) m ON true
WHERE r.server_id IS NOT NULL;
