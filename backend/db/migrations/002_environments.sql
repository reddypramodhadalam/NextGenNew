-- 002_environments.sql
--
-- Applications are tested against several environments (dev, test, stage,
-- production). A test case must not know which one — it is written once and
-- run anywhere — so the URL belongs to the environment, not the test.
--
-- Purely additive: no existing column changes, no data migration needed.
-- Applications that already have a base_url are given a "default"
-- environment from it, so nothing breaks.

CREATE TABLE IF NOT EXISTS application_environments (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, name)
);

CREATE INDEX IF NOT EXISTS idx_app_env_application
    ON application_environments (application_id);

-- Which environment a run actually used. "This test passed" means nothing
-- without it, and an auditor will ask.
ALTER TABLE executions
    ADD COLUMN IF NOT EXISTS environment TEXT;

-- Seed a default environment for every application that already has a URL.
INSERT INTO application_environments (application_id, name, base_url, is_default)
SELECT a.id, 'default', a.base_url, TRUE
FROM applications a
WHERE a.base_url IS NOT NULL
  AND a.base_url <> ''
  AND NOT EXISTS (
      SELECT 1 FROM application_environments e WHERE e.application_id = a.id
  );
