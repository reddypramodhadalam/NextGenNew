-- migrations/012_help_and_support.sql
--
-- Help & Support schema — the in-product resource centre.
--
-- Five additions, all new tables plus one widened CHECK. Nothing existing is
-- altered or dropped, so this migration cannot affect the generation, sync,
-- execution or scheduling paths.
--
-- 1. help_articles        Versioned how-to content, authored as markdown and
--                         seeded from backend/content/help/*.md. Treated as a
--                         CONTROLLED DOCUMENT: an article is never edited in
--                         place. A change inserts a new row at version+1 and
--                         marks the previous one is_stale with superseded_by
--                         pointing forward — the same soft-delete discipline
--                         used for test cases, for the same GxP reason (an
--                         auditor must be able to see what the text said on
--                         the day someone read it).
--
-- 2. help_chunks          One row per retrievable passage, from EITHER an
--                         article OR an uploaded training document. This is
--                         the retrieval unit: search ranks chunks, not whole
--                         documents, so NextGen2 AI receives the three relevant
--                         paragraphs instead of a twelve-page PDF.
--
--                         Chunking at write time is deliberate. Today the
--                         chunks are ranked by Postgres full-text search. When
--                         embeddings are introduced later (Qdrant or any other
--                         vector store), the SAME chunk rows become the points
--                         that get embedded — chunk_id is the join key. That
--                         makes the vector step additive rather than a
--                         re-derivation of the corpus.
--
-- 3. help_views           The training record: who opened which article or
--                         training document, when, and whether they
--                         acknowledged having read it. This is the audit
--                         artefact — training evidence, exportable as CSV.
--
-- 4. support_tickets      Local mirror of a ticket raised from inside NextGen2.
--                         Deliberately PROVIDER-AGNOSTIC: `provider` plus an
--                         opaque `external_ref`/`external_url`, never a
--                         jira_key column. Moving from Jira to ServiceNow is
--                         then a new provider class and a config change, with
--                         no schema migration and no data rewrite.
--
-- 5. help_search_misses   Every query that returned nothing useful. This is
--                         the documentation backlog captured as data rather
--                         than guessed at.
--
-- Plus: documents.document_type gains 'training', so an uploaded training PDF
-- reuses the existing documents table, storage path and text extraction
-- (service/orchestrator.py::_extract_text_from_document) rather than
-- introducing a parallel upload path.
--
-- Requires PostgreSQL 12+ (uses a GENERATED ALWAYS ... STORED column for the
-- tsvector). Safe to re-run: IF NOT EXISTS throughout and guarded constraints.


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. help_articles
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS help_articles (
    id              SERIAL PRIMARY KEY,

    -- Stable human-readable id, e.g. 'scheduling-a-run'. Carried across
    -- versions: every version of an article shares its slug, and exactly one
    -- of them is live at a time (enforced by ux_help_articles_live below).
    slug            TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    summary         TEXT,
    body_md         TEXT NOT NULL,

    -- Controlled-document fields. An article is only retrievable by NextGen2 AI
    -- and only counts as training material once status = 'approved'; a draft
    -- is visible in the UI, clearly marked, but is never cited as authority.
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT    NOT NULL DEFAULT 'draft',
    author          TEXT,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    effective_from  TIMESTAMPTZ,

    -- Ordering within a category, so the navigation reads in a teaching order
    -- rather than alphabetically.
    sort_order      INTEGER NOT NULL DEFAULT 100,

    -- Soft delete / supersede. History is retained, never removed.
    is_stale        BOOLEAN NOT NULL DEFAULT FALSE,
    superseded_by   INTEGER REFERENCES help_articles(id),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    ALTER TABLE help_articles
        ADD CONSTRAINT help_articles_status_check
        CHECK (status IN ('draft', 'approved', 'retired'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Exactly one live version per slug. Superseded versions stay in the table
-- with is_stale = TRUE and do not collide.
CREATE UNIQUE INDEX IF NOT EXISTS ux_help_articles_live
    ON help_articles (slug) WHERE is_stale = FALSE;

CREATE INDEX IF NOT EXISTS ix_help_articles_category
    ON help_articles (category, sort_order) WHERE is_stale = FALSE;

COMMENT ON TABLE help_articles IS
    'Versioned help content, treated as a controlled document: never edited in place, superseded via is_stale/superseded_by.';
COMMENT ON COLUMN help_articles.status IS
    'draft | approved | retired. Only approved articles are cited by NextGen2 AI or counted as training material.';


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. help_chunks — the retrieval unit
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS help_chunks (
    id              SERIAL PRIMARY KEY,

    -- Which kind of source this passage came from. Both kinds are searched
    -- together so a user's question is answered from whichever explains it
    -- best, article or training deck.
    source_type     TEXT NOT NULL,

    -- Exactly one of these is set, matching source_type (see the CHECK below).
    article_id      INTEGER REFERENCES help_articles(id) ON DELETE CASCADE,
    document_id     INTEGER REFERENCES documents(id)     ON DELETE CASCADE,

    chunk_index     INTEGER NOT NULL,      -- position within its source
    heading         TEXT,                  -- nearest heading, shown as the citation label
    body            TEXT NOT NULL,         -- the passage itself

    -- Full-text index over heading + body. A stored generated column rather
    -- than a trigger: it cannot drift out of step with the text.
    search_vector   tsvector GENERATED ALWAYS AS (
                        to_tsvector('english'::regconfig,
                                    coalesce(heading, '') || ' ' || coalesce(body, ''))
                    ) STORED,

    -- Chunks are rebuilt wholesale when their source is re-seeded or a
    -- document is re-extracted; the old set is marked stale rather than
    -- deleted so a citation recorded in help_views still resolves.
    is_stale        BOOLEAN NOT NULL DEFAULT FALSE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    ALTER TABLE help_chunks
        ADD CONSTRAINT help_chunks_source_type_check
        CHECK (source_type IN ('article', 'training_doc'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- A chunk belongs to exactly one source, and that source matches source_type.
-- Without this it is possible to insert an orphan chunk that search would
-- return but no citation could resolve.
DO $$
BEGIN
    ALTER TABLE help_chunks
        ADD CONSTRAINT help_chunks_one_source
        CHECK (
            (source_type = 'article'      AND article_id IS NOT NULL AND document_id IS NULL)
         OR (source_type = 'training_doc' AND document_id IS NOT NULL AND article_id IS NULL)
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_help_chunks_fts
    ON help_chunks USING GIN (search_vector);

CREATE INDEX IF NOT EXISTS ix_help_chunks_article
    ON help_chunks (article_id, chunk_index) WHERE is_stale = FALSE;

CREATE INDEX IF NOT EXISTS ix_help_chunks_document
    ON help_chunks (document_id, chunk_index) WHERE is_stale = FALSE;

COMMENT ON TABLE help_chunks IS
    'One retrievable passage per row, from an article or a training document. Ranked by full-text search today; the same rows become vector points when embeddings are added.';


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. help_views — the training record
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS help_views (
    id                 SERIAL PRIMARY KEY,

    -- Exactly one of these is set — the article or the training document that
    -- was opened.
    article_id         INTEGER REFERENCES help_articles(id) ON DELETE SET NULL,
    document_id        INTEGER REFERENCES documents(id)     ON DELETE SET NULL,

    -- The version that was actually on screen. Recorded as a value, not a
    -- lookup, so the record still reads true after the article is superseded.
    article_version    INTEGER,

    viewed_by          TEXT NOT NULL DEFAULT 'anonymous',
    viewed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- An explicit "I have read and understood" action. A view alone is not
    -- training evidence; an acknowledgement is.
    acknowledged       BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at    TIMESTAMPTZ
);

DO $$
BEGIN
    ALTER TABLE help_views
        ADD CONSTRAINT help_views_one_source
        CHECK (
            (article_id IS NOT NULL AND document_id IS NULL)
         OR (document_id IS NOT NULL AND article_id IS NULL)
        );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_help_views_by_user
    ON help_views (viewed_by, viewed_at DESC);

CREATE INDEX IF NOT EXISTS ix_help_views_acknowledged
    ON help_views (acknowledged, acknowledged_at DESC) WHERE acknowledged = TRUE;

COMMENT ON TABLE help_views IS
    'Training record: who opened which help article or training document, at which version, and whether they acknowledged reading it.';


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. support_tickets — provider-agnostic
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS support_tickets (
    id              SERIAL PRIMARY KEY,

    -- Which ticketing system this was raised into. The value matches a key in
    -- service/ticketing/registry.py. There is deliberately no jira_key column:
    -- the external system is a runtime choice, not a schema commitment.
    provider        TEXT NOT NULL,
    external_ref    TEXT,          -- e.g. 'KVSUP-14' (Jira) or 'INC0012345' (ServiceNow)
    external_url    TEXT,          -- deep link back to the ticket

    raised_by       TEXT NOT NULL DEFAULT 'anonymous',

    -- Plain-English category chosen by the user. The provider maps this onto
    -- its own object model (Jira issue type, ServiceNow incident vs request),
    -- so the user is never asked to understand ITIL.
    category        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    description     TEXT,

    -- Automatically captured technical context: route, application_id,
    -- execution_id, browser, last error. Collected by the UI so the ticket is
    -- actionable without a follow-up conversation.
    context         JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Local lifecycle mirror. The external system remains the system of
    -- record; this exists so the user can see their tickets inside NextGen2.
    status          TEXT NOT NULL DEFAULT 'open',
    external_status TEXT,
    last_synced_at  TIMESTAMPTZ,

    -- Set when the provider call fails, so a ticket is never silently lost:
    -- the row is written either way and the failure is visible.
    submit_error    TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    ALTER TABLE support_tickets
        ADD CONSTRAINT support_tickets_status_check
        CHECK (status IN ('open', 'submitted', 'failed', 'resolved', 'closed'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_support_tickets_by_user
    ON support_tickets (raised_by, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_support_tickets_external
    ON support_tickets (provider, external_ref);

COMMENT ON TABLE support_tickets IS
    'Local mirror of tickets raised from NextGen2. Provider-agnostic by design: switching Jira to ServiceNow is a new provider class and a config change, not a migration.';
COMMENT ON COLUMN support_tickets.context IS
    'Auto-captured technical context (route, application_id, execution_id, browser, last error) so the ticket is actionable on arrival.';


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. help_search_misses — the documentation backlog, as data
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS help_search_misses (
    id              SERIAL PRIMARY KEY,
    query           TEXT NOT NULL,

    -- Where the question was asked: the Help search box, or NextGen2 AI.
    source          TEXT NOT NULL DEFAULT 'help_search',

    -- 0 means nothing matched at all; a small number means the results existed
    -- but scored below the usefulness threshold. Both are content gaps.
    result_count    INTEGER NOT NULL DEFAULT 0,

    asked_by        TEXT NOT NULL DEFAULT 'anonymous',
    asked_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    ALTER TABLE help_search_misses
        ADD CONSTRAINT help_search_misses_source_check
        CHECK (source IN ('help_search', 'chat'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS ix_help_search_misses_recent
    ON help_search_misses (asked_at DESC);

COMMENT ON TABLE help_search_misses IS
    'Queries that returned nothing useful — the documentation gap list, captured rather than guessed.';


-- ─────────────────────────────────────────────────────────────────────────────
-- 6. documents.document_type gains 'training'
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Training PDFs and decks reuse the existing documents table: same storage
-- path, same extraction (_extract_text_from_document already handles PDF via
-- PyMuPDF and DOCX via python-docx), same lifecycle. Only the CHECK widens.
--
-- The constraint was declared inline in schema.sql, so PostgreSQL auto-named
-- it documents_document_type_check. Dropped by that name and re-added.

ALTER TABLE documents
    DROP CONSTRAINT IF EXISTS documents_document_type_check;

DO $$
BEGIN
    ALTER TABLE documents
        ADD CONSTRAINT documents_document_type_check
        CHECK (document_type IN
               ('requirements', 'existing_test_cases', 'standard', 'training'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN documents.document_type IS
    'requirements | existing_test_cases | standard | training. training = help/onboarding material shown in Help & Support and retrieved by NextGen2 AI.';
