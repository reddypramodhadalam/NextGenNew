-- 006_test_case_steps.sql
--
-- A test case is a title AND the steps that carry it out. Until now the only
-- place steps existed was as flattened text inside description, produced by
-- the spreadsheet importer (service/test_case_import.py). The script
-- generator reads description, so that flattening stays — but the structured
-- form has to live somewhere it can be edited, exported and traced back to a
-- requirement.
--
-- Purely additive:
--   * every new column is NULLABLE with no default, so Postgres adds a
--     catalog entry and rewrites no rows. Existing rows keep their exact
--     current meaning: steps IS NULL means "one-line test case", which is
--     precisely what they are.
--   * the widened CHECK is a strict superset of the old one, so no existing
--     row can violate it — the same reasoning as 005_generation_status.sql.
--
-- Nothing is deleted, rewritten or backfilled.

-- The structured source of truth for a test case's steps.
--   [{"n": 1, "step": "Click Settings", "expected": "The menu opens",
--     "matched": true, "element_hint": "aria-label='Settings'"}]
-- description is DERIVED from this (flattened) whenever it is non-null, and
-- must never be edited independently of it.
ALTER TABLE test_case_proposals
    ADD COLUMN IF NOT EXISTS steps JSONB;

-- Which numbered step each part of the generated pytest file implements, so
-- a per-step screenshot can be attached to the step it is evidence for.
--   {"1": {"label": "open_settings", "screenshot_prefix": "step_01"}}
-- Deliberately NOT folded into generations.trace: trace is the Explorer
-- Agent's raw audit record and means something different.
ALTER TABLE generations
    ADD COLUMN IF NOT EXISTS step_map JSONB;

-- Which requirements a survey read, and which produced no test case at all.
-- Append-only array of runs: a regulated environment cannot have the previous
-- run's coverage gap silently overwritten by the next one.
--   {"runs": [{"at": "...", "requirements": ["NET-020"],
--              "zero_coverage": ["NET-031"], "chunks": 12,
--              "not_surveyed": ["NET-044"]}]}
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS requirement_index JSONB;

-- proposal_edits can now record a step edit. The table has existed since the
-- first schema but has never been written to — only deleted from. "Who
-- changed step 3, and when" is exactly the question it was created to answer.
ALTER TABLE proposal_edits
    DROP CONSTRAINT IF EXISTS proposal_edits_field_edited_check;

ALTER TABLE proposal_edits
    ADD CONSTRAINT proposal_edits_field_edited_check
    CHECK (field_edited IN ('title', 'description', 'steps'));

-- Find the test cases that carry structure without scanning every row.
CREATE INDEX IF NOT EXISTS idx_proposals_with_steps
    ON test_case_proposals (application_id)
    WHERE steps IS NOT NULL;
