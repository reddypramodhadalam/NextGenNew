-- 010_jira_sync.sql
--
-- Stage 3 (Jira change-detection / scheduled poll) schema.
--
-- Two additions:
--
-- 1. test_case_proposals.source_sync_status — the flag the poll sets and the
--    UI (Stage 4) reads. For a jira-sourced test case:
--       'current'              — the linked Jira issue has not changed since
--                                this test was generated (the default).
--       'requirement_changed'  — the Jira issue was updated AFTER this test
--                                was generated; a human must re-review it.
--       'requirement_deleted'  — the Jira issue no longer exists / left the
--                                requirement set (reserved; not set in v1).
--    The poll only FLAGS. It never regenerates or edits a test case — that
--    would be an unreviewed AI change to a validated artefact, which GxP
--    forbids. A human decides what to do with a flagged case.
--
-- 2. jira_sync_state — remembers when each application/project was last
--    synced, so the poll asks Jira only for issues updated since then
--    (JQL: updated >= last_synced_at) rather than re-reading everything.
--
-- Both are additive; nothing existing is touched. source_sync_status
-- defaults to 'current' so pre-010 rows read as not-flagged.

ALTER TABLE test_case_proposals
    ADD COLUMN IF NOT EXISTS source_sync_status TEXT NOT NULL DEFAULT 'current';

-- Guard the allowed values, matching the pattern of the other status columns.
DO $$
BEGIN
    ALTER TABLE test_case_proposals
        ADD CONSTRAINT test_case_proposals_source_sync_status_check
        CHECK (source_sync_status IN
               ('current', 'requirement_changed', 'requirement_deleted'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN test_case_proposals.source_sync_status IS
    'Change-detection flag for jira-sourced cases: current | requirement_changed | requirement_deleted. Set by the sync poll; flag-only, never auto-regenerated.';


CREATE TABLE IF NOT EXISTS jira_sync_state (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    project_key     TEXT NOT NULL,
    -- The high-water mark: the poll next asks Jira for issues updated at or
    -- after this. NULL means "never synced" — the first poll seeds it.
    last_synced_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One sync cursor per application+project pair.
    UNIQUE (application_id, project_key)
);

COMMENT ON TABLE jira_sync_state IS
    'Per application+project high-water mark for the Jira change-detection poll (Stage 3).';
