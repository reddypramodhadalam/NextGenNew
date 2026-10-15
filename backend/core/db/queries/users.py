"""
core/db/queries/users.py

All database reads and writes for the `users` table — the internal Kavach
user identity introduced in migration 016. One module per table: nothing
else touches `users` directly.

The `users` row is the stable, internal identity that every per-user feature
keys off (per-application ownership, access grants, and per-user application
credentials). It is intentionally decoupled from Microsoft Entra: the
`entra_object_id` column is the join point to Entra's stable "oid" claim, but
this module never talks to Entra itself.

DEV / INTERIM NOTE
    Until the Phase 1 backend token validation is in place, the only identity
    signal the backend receives is the caller's email (the `X-Triggered-By`
    header the frontend already stamps on every request). `get_or_create_by_
    email` therefore provisions a `users` row keyed on the email, storing the
    email itself in `entra_object_id` as a stable placeholder. When real Entra
    validation is wired up, the resolver in `auth/identity.py` will supply the
    genuine `oid` and this is the only module that needs to change — callers
    continue to receive an internal `users.id` exactly as they do today.
"""

import logging
from typing import Optional

from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def get_or_create_by_email(email: str, display_name: Optional[str] = None) -> dict:
    """Return the internal `users` row for this email, creating it if needed.

    Keyed on `entra_object_id` so exactly one row ever exists per identity
    (the UNIQUE constraint enforces it via ON CONFLICT). In this interim
    phase the email doubles as the `entra_object_id`; see the module
    docstring.

    Never overwrites `is_admin` (that flag is managed manually in the DB per
    the Phase 2 Option C design) and never blanks an existing `display_name`.

    Args:
        email: The caller's email / UPN (from the identity header).
        display_name: Optional human-friendly name to seed on first sight.

    Returns:
        A dict with id, entra_object_id, email, display_name, is_admin.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        raise ValueError("email is required to resolve a user")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (entra_object_id, email, display_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (entra_object_id) DO UPDATE
                    SET email        = EXCLUDED.email,
                        display_name = COALESCE(EXCLUDED.display_name, users.display_name),
                        updated_at   = now()
                RETURNING id, entra_object_id, email, display_name, is_admin
                """,
                (normalized, normalized, (display_name or "").strip() or None),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_by_id(user_id: int) -> Optional[dict]:
    """Return a `users` row by its internal id, or None if it does not exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, entra_object_id, email, display_name, is_admin
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def get_by_email(email: str) -> Optional[dict]:
    """Return an EXISTING `users` row for this email, or None — never creates.

    Used on the run path (crawl/generation/execution) to map the initiator's
    email to their internal id WITHOUT provisioning a row: a run must never
    create users as a side effect. By the time a run uses a per-user
    credential, the user already exists (they created it by saving that
    credential through the Stage 2a route).

    Email is not unique at the schema level, so the lowest id is returned for
    determinism in the unlikely event of duplicates.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, entra_object_id, email, display_name, is_admin
                FROM users
                WHERE email = %s
                ORDER BY id
                LIMIT 1
                """,
                (normalized,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))
