-- 015_generations_map_id.sql
--
-- Track which App Map was used to generate each script.
--
-- CLAUDE.md requirement:
--   "Write the map ID used for generation into the generations row so there
--   is a permanent record of which element map each script was generated from.
--   The healer reads the map stored at generation time so it can compare old
--   state vs new state when proposing a replacement."
--
-- Without this column the healer has no way to retrieve the element snapshot
-- that was current when the script was written, making before/after comparison
-- impossible.  NULL means the generation pre-dates this migration or ran
-- without a crawled map (live-page extraction fallback).

ALTER TABLE generations
    ADD COLUMN IF NOT EXISTS app_map_id INTEGER REFERENCES application_maps(id);

COMMENT ON COLUMN generations.app_map_id IS
    'FK to application_maps — the element map snapshot used when this script '
    'was generated.  NULL for generations that pre-date this column or that '
    'fell back to live-page extraction (no crawled map existed for the app).';

CREATE INDEX IF NOT EXISTS idx_generations_app_map
    ON generations (app_map_id)
    WHERE app_map_id IS NOT NULL;
