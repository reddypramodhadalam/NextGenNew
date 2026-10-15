-- 008_test_case_provenance.sql
--
-- Traceability metadata for a test case: WHERE it came from, and WHETHER a
-- human/document specified it or the AI reasoned it. In a regulated context an
-- auditor asks exactly this — "this test, who or what authored it, and what
-- did it derive from" — so it has to be stored on the record, not inferred.
--
-- Note on naming: the existing `source` column already means the INTERNAL
-- producer ('author_agent' | 'bulk_import' | 'brd_frd') and is left untouched.
-- `origin` is the REAL-WORLD provenance (a BRD document, a crawl, a Jira
-- story, an ALM record). Two different concepts, two columns — overloading
-- `source` would break its CHECK and conflate the two meanings.
--
-- Purely additive, same reasoning as 006:
--   * every new column is NULLABLE with no default, so Postgres adds a catalog
--     entry and rewrites no rows. Existing rows keep their exact meaning
--     (NULL derivation/origin = "recorded before provenance was tracked").
--   * the CHECKs allow NULL, so no existing row can violate them.
--
-- Nothing is deleted, rewritten or backfilled. created_at / updated_at already
-- exist and are reused as generated-at / last-updated — no new timestamp
-- columns are added.

-- Did the requirement STATE this case, or did the model REASON it?
--   'stated'   — written in the requirement text or its acceptance criterion.
--   'inferred' — the AI derived it from the behaviour type; the document did
--                not spell it out. This is the governance flag: it lets a
--                human see, and reject, the AI's guesses.
ALTER TABLE test_case_proposals
    ADD COLUMN IF NOT EXISTS derivation TEXT;

ALTER TABLE test_case_proposals
    DROP CONSTRAINT IF EXISTS test_case_proposals_derivation_check;

ALTER TABLE test_case_proposals
    ADD CONSTRAINT test_case_proposals_derivation_check
    CHECK (derivation IN ('stated', 'inferred') OR derivation IS NULL);

-- The real-world artifact this test case traces back to. 'brd' today; the
-- other values become live as their source adapters are built.
ALTER TABLE test_case_proposals
    ADD COLUMN IF NOT EXISTS origin TEXT;

ALTER TABLE test_case_proposals
    DROP CONSTRAINT IF EXISTS test_case_proposals_origin_check;

ALTER TABLE test_case_proposals
    ADD CONSTRAINT test_case_proposals_origin_check
    CHECK (origin IN ('brd', 'crawl', 'jira', 'alm') OR origin IS NULL);

-- Which version of the source this was generated from — a BRD version string
-- ("v0.9"), a document hash, or a Jira story version. NULL when unknown.
-- Sources change over time; without this you cannot tell whether a test was
-- generated from the current requirement or a superseded one.
ALTER TABLE test_case_proposals
    ADD COLUMN IF NOT EXISTS source_version TEXT;
