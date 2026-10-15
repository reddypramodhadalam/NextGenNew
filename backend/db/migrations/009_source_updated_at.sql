-- 009_source_updated_at.sql
--
-- Adds source_updated_at to test_case_proposals.
--
-- For a Jira-sourced test case, this stores the Jira issue's own `updated`
-- timestamp AT THE MOMENT the test case was generated from it. It is the
-- anchor for change-detection: a scheduled poll (Stage 3) compares the Jira
-- issue's current `updated` against this value — if Jira's is newer, the
-- requirement changed after the test was written, and the linked test cases
-- are flagged for human re-review (never auto-regenerated, per GxP).
--
-- NULL for BRD/crawl-sourced rows, and for every pre-009 row — the column is
-- additive and nullable, so it does not touch existing data.
--
-- Deliberately a separate column from source_version: source_version means
-- "which revision of the source spec" (e.g. a BRD "v2.1"), a different fact
-- from "when the source last changed". Overloading one column with both would
-- be a trap for the next reader and for an auditor.

ALTER TABLE test_case_proposals
    ADD COLUMN IF NOT EXISTS source_updated_at TIMESTAMPTZ;

COMMENT ON COLUMN test_case_proposals.source_updated_at IS
    'For jira-sourced cases: the source issue''s updated timestamp at generation time. Anchor for change-detection. NULL for other sources.';
