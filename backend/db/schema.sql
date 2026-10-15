-- NextGen2 Database Schema
-- Run once to initialise:
--   psql -U postgres -d nextgen2 -f db/schema.sql

-- Internal Kavach users mapped to Microsoft Entra object IDs.
-- is_admin is managed in the database; access enforcement lives in application code.
CREATE TABLE users (
    id                SERIAL PRIMARY KEY,
    entra_object_id   TEXT NOT NULL UNIQUE,
    email             TEXT NOT NULL,
    display_name      TEXT,
    is_admin          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per application under test.
CREATE TABLE applications (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    base_url        TEXT,
    requires_sso_gateway BOOLEAN NOT NULL DEFAULT FALSE,
    normalized_url  TEXT UNIQUE,
    owner           TEXT,
    -- Nullable until assigned; the existing owner text remains informational.
    owner_user_id   INTEGER REFERENCES users(id),
    is_archived     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-application, per-user permission grants. Matches migration 016.
CREATE TABLE application_access (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    can_view        BOOLEAN NOT NULL DEFAULT TRUE,
    can_edit        BOOLEAN NOT NULL DEFAULT FALSE,
    can_run         BOOLEAN NOT NULL DEFAULT FALSE,
    can_schedule    BOOLEAN NOT NULL DEFAULT FALSE,
    can_delete      BOOLEAN NOT NULL DEFAULT FALSE,
    granted_by      INTEGER REFERENCES users(id),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, user_id)
);

-- Per-user application login credentials. Matches migration 017.
-- One credential row per (application, user); the password is encrypted at
-- rest with Fernet using TEST_DATA_ENCRYPTION_KEY (never stored in plaintext).
CREATE TABLE app_credentials (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username        TEXT NOT NULL,
    secret_alg      TEXT NOT NULL DEFAULT 'fernet',
    secret_cipher   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, user_id)
);

-- App Map: persistent record of every page and interactive element crawled
-- from an application session (authenticated or not).
-- One row per crawl run. Old rows are kept for audit history.
CREATE TABLE application_maps (
    id                SERIAL PRIMARY KEY,
    application_id    INTEGER NOT NULL REFERENCES applications(id),
    crawled_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    page_count        INTEGER NOT NULL DEFAULT 0,
    element_count     INTEGER NOT NULL DEFAULT 0,
    map_data          JSONB NOT NULL DEFAULT '{}',
    -- True if the crawl authenticated (elements behind login wall are included).
    was_authenticated BOOLEAN NOT NULL DEFAULT FALSE,
    -- 'running' while in progress; 'complete'; 'failed'.
    status            TEXT NOT NULL DEFAULT 'complete'
                          CHECK (status IN ('running', 'complete', 'failed')),
    error_message     TEXT
);

-- Stores uploaded documents: BRD/FRD requirements, existing test case files,
-- and knowledge base standards. Keeps both original file and extracted text.
CREATE TABLE documents (
    id                 SERIAL PRIMARY KEY,
    application_id     INTEGER REFERENCES applications(id),
    document_type      TEXT NOT NULL CHECK (document_type IN ('requirements', 'existing_test_cases', 'standard', 'training')),
    original_filename  TEXT NOT NULL,
    storage_path       TEXT NOT NULL,
    extracted_text     TEXT,
    -- Which requirements a survey read and which produced no test case.
    -- Append-only: see 006_test_case_steps.sql.
    requirement_index  JSONB,
    uploaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per file-upload import operation.
CREATE TABLE import_batches (
    id                  SERIAL PRIMARY KEY,
    application_id      INTEGER NOT NULL REFERENCES applications(id),
    document_id         INTEGER REFERENCES documents(id),
    file_name           TEXT NOT NULL,
    file_type           TEXT NOT NULL CHECK (file_type IN ('csv', 'xlsx', 'json')),
    imported_row_count  INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('completed', 'partial_failure', 'failed')),
    imported_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per proposed test scenario.
CREATE TABLE test_case_proposals (
    id                  SERIAL PRIMARY KEY,
    application_id      INTEGER NOT NULL REFERENCES applications(id),
    import_batch_id     INTEGER REFERENCES import_batches(id),
    source_document_id  INTEGER REFERENCES documents(id),
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    -- Structured steps: [{"n":1,"step":...,"expected":...,"matched":bool}]
    -- description is a flattened projection of this when it is non-null.
    -- NULL means a one-line test case, which is what all pre-006 rows are.
    steps               JSONB,
    test_type           TEXT NOT NULL DEFAULT 'functional',
    category            TEXT CHECK (category IN ('positive', 'negative', 'edge', 'boundary', 'wrong_data', 'missing_field', 'security', NULL)),
    source              TEXT NOT NULL CHECK (source IN ('author_agent', 'bulk_import', 'brd_frd')),
    priority            TEXT CHECK (priority IN ('P0', 'P1', 'P2', 'P3', 'P4', NULL)),
    derivation          TEXT CHECK (derivation IN ('stated', 'inferred') OR derivation IS NULL),
    origin              TEXT CHECK (origin IN ('brd', 'crawl', 'jira', 'alm') OR origin IS NULL),
    source_version      TEXT,
    source_updated_at   TIMESTAMPTZ,
    source_sync_status  TEXT NOT NULL DEFAULT 'current'
                            CHECK (source_sync_status IN ('current', 'requirement_changed', 'requirement_deleted')),
    tags                JSONB,
    should_automate     BOOLEAN,
    status              TEXT NOT NULL DEFAULT 'proposed'
                            CHECK (status IN ('proposed', 'queued', 'generating', 'generated', 'stale', 'manual_only', 'archived')),
    raw_import_row      JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Version history for edits to a scenario's text.
CREATE TABLE proposal_edits (
    id                      SERIAL PRIMARY KEY,
    test_case_proposal_id   INTEGER NOT NULL REFERENCES test_case_proposals(id),
    field_edited            TEXT NOT NULL CHECK (field_edited IN ('title', 'description', 'steps')),
    previous_value          TEXT NOT NULL,
    new_value               TEXT NOT NULL,
    edited_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    edited_by               TEXT
);

-- One row per automation attempt for a proposal.
-- script_content is the source of truth — script_path is derived from it.
CREATE TABLE generations (
    id                      SERIAL PRIMARY KEY,
    test_case_proposal_id   INTEGER NOT NULL REFERENCES test_case_proposals(id),
    attempt_number          INTEGER NOT NULL DEFAULT 1,
    provider                TEXT NOT NULL,
    model                   TEXT NOT NULL,
    triggered_by            TEXT,
    -- 'running' is written at insert time, before the agent runs, so an
    -- interrupted generation stays detectable rather than absent.
    generation_status       TEXT NOT NULL CHECK (generation_status IN ('running', 'success', 'incomplete')),
    test_verdict            TEXT CHECK (test_verdict IN ('pass', 'fail', NULL)),
    script_content          TEXT,
    script_path             TEXT,
    steps_taken             INTEGER,
    trace                   JSONB,
    -- Which numbered test-case step each pytest block implements, so
    -- per-step screenshots attach to the step they evidence.
    step_map                JSONB,
    notes                   TEXT,
    is_stale                BOOLEAN NOT NULL DEFAULT FALSE,
    is_manually_edited      BOOLEAN NOT NULL DEFAULT FALSE,
    edited_by               TEXT,
    edited_at               TIMESTAMPTZ,
    superseded_by           INTEGER REFERENCES generations(id),
    -- Which App Map snapshot was current when this script was generated.
    -- NULL for generations that pre-date this column or that fell back to
    -- live-page extraction (no crawled map existed for the app).
    -- The healer uses this to retrieve the before-state and compare with
    -- the current snapshot when proposing a selector replacement.
    app_map_id              INTEGER REFERENCES application_maps(id),
    started_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at            TIMESTAMPTZ
);

-- One row per batch run.
CREATE TABLE batches (
    id                  SERIAL PRIMARY KEY,
    application_id      INTEGER NOT NULL REFERENCES applications(id),
    environment         TEXT NOT NULL,
    triggered_by        TEXT,
    trigger_type        TEXT NOT NULL CHECK (trigger_type IN ('manual', 'scheduled')),
    status              TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    notification_sent   BOOLEAN NOT NULL DEFAULT FALSE,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ
);

-- One row per actual test run.
CREATE TABLE executions (
    id              SERIAL PRIMARY KEY,
    generation_id   INTEGER NOT NULL REFERENCES generations(id),
    batch_id        INTEGER REFERENCES batches(id),
    suite_id        INTEGER,
    triggered_by    TEXT,
    environment     TEXT,
    result          TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'skip', 'error')),
    output          TEXT,
    screenshot_path TEXT,
    -- Per-step ledger for the run: what each numbered step did, when, how
    -- long it took and which screenshot evidences it. NULL for runs that
    -- predate step-level recording. See 007_execution_step_results.sql.
    step_results    JSONB,
    duration_ms     INTEGER,
    executed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per AI-proposed fix for a broken test.
CREATE TABLE healing_proposals (
    id              SERIAL PRIMARY KEY,
    execution_id    INTEGER NOT NULL REFERENCES executions(id),
    old_selector    TEXT NOT NULL,
    proposed_role   TEXT NOT NULL,
    proposed_name   TEXT NOT NULL,
    confidence      TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    reasoning       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
    proposed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT
);

-- One row per LLM API call, for cost and performance tracking.
CREATE TABLE ai_calls (
    id                SERIAL PRIMARY KEY,
    related_to_type   TEXT NOT NULL CHECK (related_to_type IN ('survey', 'generation', 'heal')),
    related_to_id     INTEGER NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cost_usd          NUMERIC(10, 6),
    latency_ms        INTEGER,
    called_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One or more test datasets per scenario.
CREATE TABLE proposal_data_sets (
    id                      SERIAL PRIMARY KEY,
    test_case_proposal_id   INTEGER NOT NULL REFERENCES test_case_proposals(id),
    data                    JSONB NOT NULL,
    tried                   BOOLEAN NOT NULL DEFAULT FALSE,
    result                  TEXT CHECK (result IN ('pass', 'fail', NULL)),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-application execution environments (dev/test/stage/prod).
CREATE TABLE application_environments (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    name            TEXT NOT NULL,
    base_url        TEXT NOT NULL,
    is_default      BOOLEAN NOT NULL DEFAULT FALSE,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, name)
);

-- Named sets of test cases (smoke/regression/custom).
CREATE TABLE test_suites (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id),
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'custom'
                    CHECK (kind IN ('smoke', 'regression', 'custom')),
    description     TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, name)
);

CREATE TABLE test_suite_members (
    suite_id                INTEGER NOT NULL REFERENCES test_suites(id) ON DELETE CASCADE,
    test_case_proposal_id   INTEGER NOT NULL REFERENCES test_case_proposals(id) ON DELETE CASCADE,
    added_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (suite_id, test_case_proposal_id)
);

-- Scheduled suite runs and periodic jobs.
CREATE TABLE schedules (
    id                SERIAL PRIMARY KEY,
    application_id    INTEGER NOT NULL REFERENCES applications(id),
    suite_id          INTEGER REFERENCES test_suites(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    frequency         TEXT NOT NULL DEFAULT 'daily'
                      CHECK (frequency IN ('once', 'hourly', 'daily', 'weekdays', 'weekly')),
    run_at            TIMESTAMPTZ NOT NULL,
    next_run_at       TIMESTAMPTZ,
    environment       TEXT,
    webhook_url       TEXT,
    recipients        TEXT,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at       TIMESTAMPTZ,
    last_status       TEXT,
    last_summary      JSONB,
    job_type          TEXT NOT NULL DEFAULT 'suite_run'
                      CHECK (job_type IN ('suite_run', 'jira_sync')),
    jira_project_key  TEXT,
    created_by        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (job_type <> 'jira_sync' OR jira_project_key IS NOT NULL)
);

CREATE TABLE schedule_runs (
    id              SERIAL PRIMARY KEY,
    schedule_id     INTEGER NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    total           INTEGER DEFAULT 0,
    passed          INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    skipped         INTEGER DEFAULT 0,
    triggered_by    TEXT DEFAULT 'scheduler',
    webhook_status  TEXT,
    summary         JSONB
);

-- Jira sync cursor per application/project.
CREATE TABLE jira_sync_state (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    project_key     TEXT NOT NULL,
    last_synced_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (application_id, project_key)
);

-- Versioned help content.
CREATE TABLE help_articles (
    id              SERIAL PRIMARY KEY,
    slug            TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    summary         TEXT,
    body_md         TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'approved', 'retired')),
    author          TEXT,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    effective_from  TIMESTAMPTZ,
    sort_order      INTEGER NOT NULL DEFAULT 100,
    is_stale        BOOLEAN NOT NULL DEFAULT FALSE,
    superseded_by   INTEGER REFERENCES help_articles(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Searchable passages from help content and training docs.
CREATE TABLE help_chunks (
    id              SERIAL PRIMARY KEY,
    source_type     TEXT NOT NULL CHECK (source_type IN ('article', 'training_doc')),
    article_id      INTEGER REFERENCES help_articles(id) ON DELETE CASCADE,
    document_id     INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    heading         TEXT,
    body            TEXT NOT NULL,
    search_vector   tsvector GENERATED ALWAYS AS (
                      to_tsvector('english'::regconfig,
                          coalesce(heading, '') || ' ' || coalesce(body, ''))
                    ) STORED,
    is_stale        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (source_type = 'article' AND article_id IS NOT NULL AND document_id IS NULL)
        OR
        (source_type = 'training_doc' AND document_id IS NOT NULL AND article_id IS NULL)
    )
);

-- Read/ack evidence for help/training.
CREATE TABLE help_views (
    id                 SERIAL PRIMARY KEY,
    article_id         INTEGER REFERENCES help_articles(id) ON DELETE SET NULL,
    document_id        INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    article_version    INTEGER,
    viewed_by          TEXT NOT NULL DEFAULT 'anonymous',
    viewed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged       BOOLEAN NOT NULL DEFAULT FALSE,
    acknowledged_at    TIMESTAMPTZ,
    CHECK (
        (article_id IS NOT NULL AND document_id IS NULL)
        OR
        (document_id IS NOT NULL AND article_id IS NULL)
    )
);

-- Provider-agnostic local mirror of support tickets.
CREATE TABLE support_tickets (
    id              SERIAL PRIMARY KEY,
    provider        TEXT NOT NULL,
    external_ref    TEXT,
    external_url    TEXT,
    raised_by       TEXT NOT NULL DEFAULT 'anonymous',
    category        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    description     TEXT,
    context         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'submitted', 'failed', 'resolved', 'closed')),
    external_status TEXT,
    last_synced_at  TIMESTAMPTZ,
    submit_error    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Search queries that returned no useful help results.
CREATE TABLE help_search_misses (
    id              SERIAL PRIMARY KEY,
    query           TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'help_search'
                    CHECK (source IN ('help_search', 'chat')),
    result_count    INTEGER NOT NULL DEFAULT 0,
    asked_by        TEXT NOT NULL DEFAULT 'anonymous',
    asked_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE executions
    ADD CONSTRAINT executions_suite_id_fkey
    FOREIGN KEY (suite_id) REFERENCES test_suites(id);

-- Indexes
CREATE INDEX idx_users_entra_object_id ON users(entra_object_id);
CREATE INDEX idx_applications_owner ON applications(owner_user_id);
CREATE INDEX idx_application_access_app ON application_access(application_id);
CREATE INDEX idx_application_access_user ON application_access(user_id);
CREATE INDEX idx_app_credentials_app_user ON app_credentials(application_id, user_id);
CREATE INDEX idx_proposals_application ON test_case_proposals(application_id);
CREATE INDEX idx_proposals_status ON test_case_proposals(status);
CREATE INDEX idx_proposals_document ON test_case_proposals(source_document_id);
CREATE INDEX idx_documents_application ON documents(application_id);
CREATE INDEX idx_application_maps_app_crawled ON application_maps(application_id, crawled_at DESC);
CREATE INDEX idx_generations_proposal ON generations(test_case_proposal_id);
CREATE INDEX idx_generations_app_map ON generations(app_map_id) WHERE app_map_id IS NOT NULL;
CREATE INDEX idx_executions_generation ON executions(generation_id);
CREATE INDEX idx_executions_batch ON executions(batch_id);
CREATE INDEX idx_executions_with_steps ON executions(generation_id)
    WHERE step_results IS NOT NULL;
CREATE INDEX idx_batches_application ON batches(application_id);
CREATE INDEX idx_healing_execution ON healing_proposals(execution_id);
CREATE INDEX idx_ai_calls_related ON ai_calls(related_to_type, related_to_id);
CREATE INDEX idx_data_sets_proposal ON proposal_data_sets(test_case_proposal_id);
CREATE INDEX idx_proposals_with_steps ON test_case_proposals(application_id)
    WHERE steps IS NOT NULL;
CREATE INDEX idx_app_env_application ON application_environments(application_id);
CREATE INDEX idx_suite_application ON test_suites(application_id);
CREATE INDEX idx_suite_members_suite ON test_suite_members(suite_id);
CREATE INDEX idx_schedules_due ON schedules(enabled, next_run_at);
CREATE INDEX idx_schedule_runs ON schedule_runs(schedule_id, started_at DESC);
CREATE UNIQUE INDEX ux_help_articles_live ON help_articles(slug) WHERE is_stale = FALSE;
CREATE INDEX ix_help_articles_category ON help_articles(category, sort_order) WHERE is_stale = FALSE;
CREATE INDEX ix_help_chunks_fts ON help_chunks USING GIN(search_vector);
CREATE INDEX ix_help_chunks_article ON help_chunks(article_id, chunk_index) WHERE is_stale = FALSE;
CREATE INDEX ix_help_chunks_document ON help_chunks(document_id, chunk_index) WHERE is_stale = FALSE;
CREATE INDEX ix_help_views_by_user ON help_views(viewed_by, viewed_at DESC);
CREATE INDEX ix_help_views_acknowledged ON help_views(acknowledged, acknowledged_at DESC) WHERE acknowledged = TRUE;
CREATE INDEX ix_support_tickets_by_user ON support_tickets(raised_by, created_at DESC);
CREATE INDEX ix_support_tickets_external ON support_tickets(provider, external_ref);
CREATE INDEX ix_help_search_misses_recent ON help_search_misses(asked_at DESC);
