-- 021_survey_runs.sql
-- Remembers the AI cost of each survey run, so each accepted test case can store its share.

CREATE TABLE IF NOT EXISTS survey_runs (
    run_id             TEXT PRIMARY KEY,
    application_id     INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    proposal_count     INTEGER NOT NULL DEFAULT 0,
    total_cost_usd     NUMERIC(14,6) NOT NULL DEFAULT 0,
    per_case_cost_usd  NUMERIC(14,6) NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Speeds up "latest survey run for this application" lookups.
CREATE INDEX IF NOT EXISTS idx_survey_runs_app_created ON survey_runs (application_id, created_at DESC);