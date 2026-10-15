-- 020_ai_cost_per_item.sql
-- Lets AI cost be shown against each test case and each script.

-- Groups all AI calls of one survey run so their cost can be shared across the test cases it produced.
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS run_id TEXT;
CREATE INDEX IF NOT EXISTS idx_ai_calls_run_id ON ai_calls (run_id) WHERE run_id IS NOT NULL;

-- Speeds up "cost of this generation" lookups for scripts.
CREATE INDEX IF NOT EXISTS idx_ai_calls_purpose_related ON ai_calls (purpose, related_to_id);

-- Stores each test case's estimated share of its survey run, filled in when the test case is accepted.
ALTER TABLE test_case_proposals ADD COLUMN IF NOT EXISTS ai_cost_usd NUMERIC(14,6);
ALTER TABLE test_case_proposals ADD COLUMN IF NOT EXISTS survey_run_id TEXT;