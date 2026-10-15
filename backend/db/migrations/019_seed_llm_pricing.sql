-- 019_seed_llm_pricing.sql
-- Seeds Baxter AI Hub gateway prices (USD per 1M tokens), taken from the Dev Portal LLM Gateway page.
-- The provider value must match PROVIDER_NAME in llm/bedrock_provider.py, and each model value
-- must match LLM_MODEL in backend/.env exactly, or cost is saved as NULL.
-- ON CONFLICT makes this safe to run more than once.
-- When a price changes, add a NEW row with a later effective_from. Never edit old rows,
-- so past calls keep the price that applied at the time.

INSERT INTO llm_model_pricing (provider, model, input_usd_per_1m, output_usd_per_1m, effective_from)
VALUES
    ('bedrock', 'gpt-5.4-nano',    0.20, 1.25, '2026-10-01T00:00:00Z'),
    ('bedrock', 'gpt-5.6-luna',    0.20, 1.20, '2026-10-01T00:00:00Z'),
    ('bedrock', 'gpt-5.4-mini',    0.75, 4.50, '2026-10-01T00:00:00Z'),
    ('bedrock', 'claude-haiku-4.5', 1.10, 5.50, '2026-10-01T00:00:00Z')
ON CONFLICT (provider, model, effective_from) DO NOTHING;