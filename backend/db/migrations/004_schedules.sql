-- 004_schedules.sql
--
-- Scheduled suite runs, and a record of each one.
--
-- next_run_at is stored rather than computed on the fly so the UI can show a
-- live countdown and the scheduler can find due work with a single indexed
-- query instead of evaluating every schedule every tick.

CREATE TABLE IF NOT EXISTS schedules (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    suite_id        INTEGER REFERENCES test_suites(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,

    -- 'once' fires a single time, which is what you want when demonstrating
    -- it live; the recurring options are the real-world use.
    frequency       TEXT NOT NULL DEFAULT 'daily'
                    CHECK (frequency IN ('once', 'hourly', 'daily', 'weekdays', 'weekly')),
    run_at          TIMESTAMPTZ NOT NULL,        -- first/next occurrence
    next_run_at     TIMESTAMPTZ,                 -- NULL once a 'once' schedule has fired

    environment     TEXT,
    webhook_url     TEXT,                        -- posted to on completion (n8n)
    recipients      TEXT,                        -- informational; n8n decides delivery
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,

    last_run_at     TIMESTAMPTZ,
    last_status     TEXT,
    last_summary    JSONB,

    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON schedules (enabled, next_run_at);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id            SERIAL PRIMARY KEY,
    schedule_id   INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    total         INTEGER DEFAULT 0,
    passed        INTEGER DEFAULT 0,
    failed        INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    triggered_by  TEXT DEFAULT 'scheduler',
    webhook_status TEXT,
    summary       JSONB
);

CREATE INDEX IF NOT EXISTS idx_schedule_runs ON schedule_runs (schedule_id, started_at DESC);
