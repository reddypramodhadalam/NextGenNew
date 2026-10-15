-- migrations/011_schedule_job_type.sql
--
-- Adds two columns to `schedules` so one scheduler can run more than one kind
-- of job:
--   job_type          discriminator the runner dispatches on ('suite_run' | 'jira_sync')
--   jira_project_key  the Jira project a jira_sync schedule targets (per-schedule
--                     data, NOT a hardcoded literal)
--
-- Existing rows become 'suite_run' automatically via the column default, so no
-- backfill is needed. Safe to re-run (IF NOT EXISTS + guarded constraint).

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS job_type TEXT NOT NULL DEFAULT 'suite_run'
    CHECK (job_type IN ('suite_run', 'jira_sync'));

ALTER TABLE schedules
    ADD COLUMN IF NOT EXISTS jira_project_key TEXT;

-- A jira_sync schedule must name a project. Guarded because Postgres has no
-- "ADD CONSTRAINT IF NOT EXISTS" for CHECK constraints.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'schedules_jira_project_required'
    ) THEN
        ALTER TABLE schedules
            ADD CONSTRAINT schedules_jira_project_required
            CHECK (job_type <> 'jira_sync' OR jira_project_key IS NOT NULL);
    END IF;
END$$;
