-- Add application-level SSO gateway toggle.
-- Source of truth for survey/generation/execute SSO prechecks.
ALTER TABLE applications
ADD COLUMN IF NOT EXISTS requires_sso_gateway BOOLEAN NOT NULL DEFAULT FALSE;
