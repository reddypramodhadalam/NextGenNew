-- 007_execution_step_results.sql
--
-- Per-step results for a run.
--
-- Until now an execution recorded one verdict and a folder of screenshots.
-- That answers "did it pass" but not "which step failed, when, and what did
-- the screen look like at that moment" — which is the question actually
-- asked when a nightly run goes red, and the one an auditor asks about a
-- test case that has approved steps.
--
--   [{"n": 1, "action": "Click the Login button",
--     "expected": "The catalogue opens", "status": "pass",
--     "started_at": "2026-07-29T14:22:07+05:30", "duration_ms": 412,
--     "screenshot": "step_01_Click_the_Login_button.png", "error": ""}]
--
-- status is one of: pass, fail, not_automated, running. 'running' survives
-- in the ledger when a run is killed mid-step, which is honest and tells you
-- exactly where it stopped.
--
-- Purely additive: one nullable column, no default, no backfill. Existing
-- executions keep step_results NULL, which correctly means "this run predates
-- step-level recording" rather than "this run had no steps".

ALTER TABLE executions
    ADD COLUMN IF NOT EXISTS step_results JSONB;

-- Find runs that carry step detail without scanning the whole table.
CREATE INDEX IF NOT EXISTS idx_executions_with_steps
    ON executions (generation_id)
    WHERE step_results IS NOT NULL;
