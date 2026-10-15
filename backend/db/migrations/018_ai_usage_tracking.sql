-- 018_ai_usage_tracking.sql
-- Extends ai_calls so every LLM call can be attributed to an activity,
-- an application and a user, and priced from a configurable pricing table.
-- Safe to re-run: every statement checks before it changes anything.

-- Which product activity triggered the call. The dashboard splits cost by this.
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'other';

-- Lets the dashboard filter by application and, later, by user.
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS application_id INTEGER;
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS user_id INTEGER;

-- FALSE means no price was configured for this model, so cost_usd is NULL.
-- This keeps "unknown cost" separate from a real $0.
ALTER TABLE ai_calls ADD COLUMN IF NOT EXISTS pricing_found BOOLEAN NOT NULL DEFAULT FALSE;

-- More room for cost values. NUMERIC keeps money exact (no float rounding).
ALTER TABLE ai_calls ALTER COLUMN cost_usd TYPE NUMERIC(14,6);

-- Some calls (e.g. chat) have no single parent row, so the link is now optional.
ALTER TABLE ai_calls ALTER COLUMN related_to_id DROP NOT NULL;

-- Allow the new related types alongside the original three.
ALTER TABLE ai_calls DROP CONSTRAINT IF EXISTS ai_calls_related_to_type_check;
ALTER TABLE ai_calls ADD CONSTRAINT ai_calls_related_to_type_check
    CHECK (related_to_type IN ('survey', 'generation', 'heal', 'chat', 'other'));

-- Only known activity names can be stored, so typos can't create new dashboard rows.
ALTER TABLE ai_calls DROP CONSTRAINT IF EXISTS ai_calls_purpose_check;
ALTER TABLE ai_calls ADD CONSTRAINT ai_calls_purpose_check
    CHECK (purpose IN ('test_case_generation', 'script_generation', 'healing',
                       'chat', 'crawl', 'other'));

-- Indexes for the dashboard query (filtered by date, purpose and application).
CREATE INDEX IF NOT EXISTS idx_ai_calls_called_at   ON ai_calls (called_at);
CREATE INDEX IF NOT EXISTS idx_ai_calls_purpose_at  ON ai_calls (purpose, called_at);
CREATE INDEX IF NOT EXISTS idx_ai_calls_app_at      ON ai_calls (application_id, called_at);

-- Price per 1 million tokens for each provider and model.
-- When a price changes, add a new row instead of editing the old one, so past
-- calls keep the price that applied at the time.
CREATE TABLE IF NOT EXISTS llm_model_pricing (
    id                 SERIAL PRIMARY KEY,
    provider           TEXT          NOT NULL,
    model              TEXT          NOT NULL,
    input_usd_per_1m   NUMERIC(12,6) NOT NULL CHECK (input_usd_per_1m  >= 0),
    output_usd_per_1m  NUMERIC(12,6) NOT NULL CHECK (output_usd_per_1m >= 0),
    effective_from     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (provider, model, effective_from)
);

-- Speeds up "latest price for this model" lookups.
CREATE INDEX IF NOT EXISTS idx_pricing_lookup
    ON llm_model_pricing (provider, model, effective_from DESC);