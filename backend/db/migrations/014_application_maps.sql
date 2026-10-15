-- 014_application_maps.sql
-- App Map: persistent record of every page and interactive element crawled
-- from an application session (authenticated or not).
--
-- One row per crawl run. Old rows are kept for audit history — they record
-- what the application's UI surface looked like at that point in time. The
-- healer uses the map stored at generation time to compare before/after states.
--
-- map_data column schema:
--   {
--     "base_url": "https://...",
--     "crawled_at": "ISO-8601",
--     "page_count": N,
--     "pages": [
--       {
--         "url": "...",
--         "title": "...",
--         "depth": N,
--         "element_count": N,
--         "iframe_count": N,
--         "elements": [
--           {tag, id, name, aria_label, placeholder, text, data_test,
--            css_class, frame_index (if inside iframe)}
--         ]
--       }
--     ]
--   }

CREATE TABLE IF NOT EXISTS application_maps (
    id                SERIAL PRIMARY KEY,
    application_id    INTEGER NOT NULL REFERENCES applications(id),
    crawled_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    page_count        INTEGER NOT NULL DEFAULT 0,
    element_count     INTEGER NOT NULL DEFAULT 0,
    map_data          JSONB NOT NULL DEFAULT '{}',
    -- True if the crawl authenticated (elements behind login wall are included).
    was_authenticated BOOLEAN NOT NULL DEFAULT FALSE,
    -- 'running' while in progress; 'complete'; 'failed'.
    -- Used to detect abandoned in-progress rows after a server restart.
    status            TEXT NOT NULL DEFAULT 'complete'
                          CHECK (status IN ('running', 'complete', 'failed')),
    error_message     TEXT
);

-- Retrieve the latest map for an application without a full table scan.
CREATE INDEX IF NOT EXISTS idx_application_maps_app_crawled
    ON application_maps (application_id, crawled_at DESC);
