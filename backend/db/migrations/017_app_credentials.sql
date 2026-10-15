-- 017_app_credentials.sql
-- Phase 2: Per-user application login credentials.
--
-- Run once (from the backend directory):
--   python db/run_migration.py db/migrations/017_app_credentials.sql
--
-- WHY THIS TABLE EXISTS
-- Today an application's login username/password lives in a single
-- test_data/applications/app_<id>.json file, owned (via _created_by) by
-- whoever saved it first. That is "one owner per application", which cannot
-- express the real requirement: a single application may be used by several
-- Kavach users, and each user must supply and use THEIR OWN credentials,
-- never a shared one.
--
-- This table stores one credential row per (application, user). Retrieval is
-- always scoped by BOTH keys, so a stage can only ever obtain the credential
-- belonging to the user who initiated that run -- never anyone else's.
--
-- SECRECY
-- The password is encrypted at rest with Fernet, using the SAME key as the
-- existing test-data store (TEST_DATA_ENCRYPTION_KEY) -- no new key, no new
-- algorithm. The Fernet token already carries its own IV and HMAC, so a
-- single ciphertext column is sufficient. The plaintext is never stored.
--
-- BACKWARD COMPATIBILITY
-- This migration only ADDS a table. It changes no existing row and no
-- existing column. Applications continue to work exactly as before until a
-- user saves a per-user credential here.

CREATE TABLE IF NOT EXISTS app_credentials (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username        TEXT NOT NULL,
    secret_alg      TEXT NOT NULL DEFAULT 'fernet',
    secret_cipher   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One credential per user per application. A second save by the same
    -- user for the same application updates the existing row (see the
    -- ON CONFLICT upsert in core/db/queries/app_credentials.py).
    UNIQUE (application_id, user_id)
);

-- The lookup every stage performs: "this application, this user".
CREATE INDEX IF NOT EXISTS idx_app_credentials_app_user
    ON app_credentials(application_id, user_id);
