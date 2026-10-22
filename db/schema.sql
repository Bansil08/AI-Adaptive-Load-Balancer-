-- Schema for the adaptive load balancer (PostgreSQL 16).
--
-- Normalised to 3NF: each table describes one entity, keyed by a surrogate id,
-- and other tables refer to it by foreign key instead of copying its columns.
-- The two performance indexes at the end were added in Phase 4; their effect
-- (EXPLAIN ANALYZE before/after on 1M+ rows) is recorded in CONCEPTS.md.
--
-- Idempotent: safe to run more than once (CREATE ... IF NOT EXISTS).

-- Backend servers the load balancer can route to.
CREATE TABLE IF NOT EXISTS servers (
    id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    address         TEXT        NOT NULL,
    port            INTEGER     NOT NULL CHECK (port BETWEEN 1 AND 65535),
    capacity_label  TEXT        NOT NULL DEFAULT 'normal',      -- e.g. 'fast', 'normal', 'slow'
    -- 'removed' is a soft delete so historical rows keep a valid server_id.
    status          TEXT        NOT NULL DEFAULT 'up'
                                CHECK (status IN ('up', 'down', 'removed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (address, port)
);

-- One row per health probe (TCP connect or HTTP GET /health).
CREATE TABLE IF NOT EXISTS health_checks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    server_id   INTEGER     NOT NULL REFERENCES servers (id) ON DELETE RESTRICT,
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_type  TEXT        NOT NULL CHECK (check_type IN ('tcp', 'http')),
    passed      BOOLEAN     NOT NULL,
    latency_ms  DOUBLE PRECISION CHECK (latency_ms >= 0)   -- NULL when the probe timed out
);

-- Resource metrics reported by each backend's agent over UDP heartbeats.
CREATE TABLE IF NOT EXISTS server_metrics (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    server_id           INTEGER     NOT NULL REFERENCES servers (id) ON DELETE RESTRICT,
    recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    cpu_percent         DOUBLE PRECISION NOT NULL CHECK (cpu_percent BETWEEN 0 AND 100),
    memory_percent      DOUBLE PRECISION NOT NULL CHECK (memory_percent BETWEEN 0 AND 100),
    active_connections  INTEGER     NOT NULL CHECK (active_connections >= 0)
);

-- One row per experiment: which strategy was tested under which scenario.
CREATE TABLE IF NOT EXISTS experiment_runs (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    strategy    TEXT        NOT NULL,
    scenario    TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,                               -- NULL while the run is in progress
    notes       TEXT,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

-- One row per client request handled by the load balancer.
CREATE TABLE IF NOT EXISTS requests (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- NULL for normal (non-experiment) traffic.
    run_id            INTEGER     REFERENCES experiment_runs (id) ON DELETE CASCADE,
    -- NULL when no backend served it (rate limited -> 429, no healthy backend -> 503),
    -- so those requests still count towards the error rate.
    server_id         INTEGER     REFERENCES servers (id) ON DELETE RESTRICT,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Any HTTP method token, not a fixed list: the LB must be able to log every
    -- request it receives (even ones it rejects with 405/501), and one row failing
    -- a CHECK would abort the DB writer's whole batched transaction.
    method            TEXT        NOT NULL CHECK (method ~ '^[A-Z]{1,16}$'),
    path              TEXT        NOT NULL,
    status_code       SMALLINT    NOT NULL CHECK (status_code BETWEEN 100 AND 599),
    response_time_ms  DOUBLE PRECISION NOT NULL CHECK (response_time_ms >= 0),
    retried           BOOLEAN     NOT NULL DEFAULT FALSE
);

-- Aggregated results of a finished run (1:1 with experiment_runs).
-- These values are derivable from `requests`, but are stored deliberately:
-- plots stay cheap and results survive the 7-day retention job on raw rows.
CREATE TABLE IF NOT EXISTS run_results (
    run_id          INTEGER PRIMARY KEY REFERENCES experiment_runs (id) ON DELETE CASCADE,
    avg_latency_ms  DOUBLE PRECISION NOT NULL CHECK (avg_latency_ms >= 0),
    p95_latency_ms  DOUBLE PRECISION NOT NULL CHECK (p95_latency_ms >= 0),
    error_rate      DOUBLE PRECISION NOT NULL CHECK (error_rate BETWEEN 0 AND 1),
    throughput_rps  DOUBLE PRECISION NOT NULL CHECK (throughput_rps >= 0)
);

-- Performance indexes (Phase 4). Postgres does NOT index foreign-key columns
-- automatically, so without these every "requests of server X in the last 15
-- minutes" or "latest metrics of server X before time t" reads the whole table.
-- Column order matters: equality column first (server_id), range/sort column
-- second (time), so one index range scan finds exactly the rows wanted, already
-- in time order.
CREATE INDEX IF NOT EXISTS requests_server_received_idx
    ON requests (server_id, received_at);
CREATE INDEX IF NOT EXISTS server_metrics_server_recorded_idx
    ON server_metrics (server_id, recorded_at);
