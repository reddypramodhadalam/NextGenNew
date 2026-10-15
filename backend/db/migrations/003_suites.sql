-- 003_suites.sql
--
-- Test suites: a named, curated selection of existing test cases.
--
-- A suite is NOT a new set of tests. "Smoke" and "regression" are not kinds
-- of test — they are decisions about WHICH tests to run and when:
--
--   smoke       is this build worth testing?   a handful, minutes, every build
--   regression  did anything break?            everything, nightly
--
-- The same test case belongs in both, so membership is many-to-many. That is
-- why a single test_type column could not express it.
--
-- Additive: nothing existing is changed.

CREATE TABLE IF NOT EXISTS test_suites (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'custom'
                    CHECK (kind IN ('smoke', 'regression', 'custom')),
    description     TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, name)
);

CREATE TABLE IF NOT EXISTS test_suite_members (
    suite_id                INTEGER NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
    test_case_proposal_id   INTEGER NOT NULL REFERENCES test_case_proposals(id) ON DELETE CASCADE,
    added_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (suite_id, test_case_proposal_id)
);

CREATE INDEX IF NOT EXISTS idx_suite_application ON test_suites (application_id);
CREATE INDEX IF NOT EXISTS idx_suite_members_suite ON test_suite_members (suite_id);

-- Which suite a run belonged to, so results can be reported per suite.
ALTER TABLE executions ADD COLUMN IF NOT EXISTS suite_id INTEGER REFERENCES test_suites(id);
