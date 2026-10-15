-- 016_user_ownership_access.sql
-- Phase 2: Internal user identity (mapped from Microsoft Entra), per-application
-- ownership, and per-user access grants.
--
-- Run once:
--   python db/run_migration.py db/migrations/016_user_ownership_access.sql
--
-- Existing applications get owner_user_id = NULL, meaning they become
-- invisible to regular users until an Admin assigns an owner or grants
-- access via application_access. Admins (users.is_admin = true) can see
-- all applications regardless of ownership -- enforced in application code,
-- not in this schema.

-- Internal Kavach user, mapped from the Microsoft Entra token's stable
-- object ID (the "oid" claim). is_admin is managed manually for now
-- (Option C) -- see .github/copilot-instructions.md.
CREATE TABLE IF NOT EXISTS users (
    id                SERIAL PRIMARY KEY,
    entra_object_id   TEXT NOT NULL UNIQUE,
    email             TEXT NOT NULL,
    display_name      TEXT,
    is_admin          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Real, enforceable owner. Nullable so existing applications are not
-- destructively altered -- they simply have no owner until one is assigned.
-- The existing free-text `owner` column is untouched and remains purely
-- informational.
ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES users(id);

-- Per-application, per-user permission grants. A row here means "this user
-- has been given access to this application, with these specific rights."
-- The owner (applications.owner_user_id) implicitly has all rights and does
-- not need a row here. Admins (users.is_admin) implicitly have all rights
-- on every application and also do not need a row here.
CREATE TABLE IF NOT EXISTS application_access (
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

-- Indexes for the access checks that will run on nearly every application,
-- test suite, execution, report, and schedule request going forward.
CREATE INDEX IF NOT EXISTS idx_users_entra_object_id ON users(entra_object_id);
CREATE INDEX IF NOT EXISTS idx_applications_owner ON applications(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_application_access_app ON application_access(application_id);
CREATE INDEX IF NOT EXISTS idx_application_access_user ON application_access(user_id);