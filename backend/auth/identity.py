"""
auth/identity.py

THE SINGLE IDENTITY DOOR for the backend.

Every protected request resolves "who is calling?" here and nowhere else.
Routes receive an internal `users.id` (an integer) and never parse identity
headers or tokens themselves. Keeping this in one place means the day the
Phase 1 Entra token validation lands, only this file changes — every route
that calls `resolve_user_id` keeps working unchanged.

INTERIM TRUST MODEL (read before relying on this for isolation)
    Right now the only identity signal is the `X-Triggered-By` header that the
    frontend stamps with the signed-in user's email (from their MSAL account).
    That header is client-supplied, so it is trustworthy enough to keep each
    user seeing only THEIR OWN data in normal use, but it is NOT a defence
    against a deliberately forged header. True server-enforced isolation
    arrives when this function validates the Entra access token and reads the
    stable `oid` claim instead of trusting a header.

    This module deliberately does NOT validate tokens yet — per
    .github/copilot-instructions.md, backend Entra token validation is a
    separate, not-yet-started piece of work and must not be faked here.
"""

import logging

from fastapi import HTTPException, Request

from core.db.queries import users as users_db

logger = logging.getLogger(__name__)

# The header the frontend's apiFetch stamps on every call with the signed-in
# user's email. A second, explicit override header is honoured only to make
# local multi-user testing possible without juggling MSAL sessions.
IDENTITY_HEADER = "X-Triggered-By"
DEV_OVERRIDE_HEADER = "X-Dev-User"


def _caller_email(request: Request) -> str:
    """Pull the caller's email from the identity headers, or "" if absent."""
    email = (request.headers.get(DEV_OVERRIDE_HEADER) or "").strip()
    if not email:
        email = (request.headers.get(IDENTITY_HEADER) or "").strip()
    if email.lower() == "anonymous":
        return ""
    return email


def resolve_current_user(request: Request) -> dict:
    """Resolve the calling user to an internal `users` row.

    Returns the full row (id, email, display_name, is_admin, ...) so callers
    that later need the admin flag can get it from the same single door.

    Raises:
        HTTPException(401): when no usable identity is present on the request.
    """
    email = _caller_email(request)
    if not email:
        raise HTTPException(
            status_code=401,
            detail="No authenticated user on request (missing identity header).",
        )
    return users_db.get_or_create_by_email(email)


def resolve_user_id(request: Request) -> int:
    """Convenience wrapper: resolve the caller to just their internal id.

    This is what the per-user credential routes use — they only ever need the
    id to scope their queries, never the rest of the identity.
    """
    return resolve_current_user(request)["id"]
