"""
core/db/queries/applications.py

All database reads and writes for the `applications` table.
No other module touches this table directly.
"""

import logging
from typing import Optional
from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def find_by_normalized_url(normalized_url: str, include_archived: bool = False) -> Optional[dict]:
    """
    Finds an existing application by its normalised URL.

    Args:
        normalized_url: The URL after normalisation (lowercase host,
            stripped trailing slash, stripped default port).
        include_archived: When True, archived rows are eligible matches.

    Returns:
        A dict with all application fields, or None if not found.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                                SELECT id, name, base_url, requires_sso_gateway, normalized_url, owner,
                       is_archived, created_at
                FROM applications
                WHERE normalized_url = %s
                  AND (%s OR is_archived = FALSE)
                """,
                (normalized_url, include_archived),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def unarchive(application_id: int, name: Optional[str], base_url: Optional[str], owner: Optional[str]) -> Optional[dict]:
    """
    Restores an archived application to active state.

    Args:
        application_id: Row ID to restore.
        name: Optional replacement display name.
        base_url: Optional replacement base URL.
        owner: Optional replacement owner.

    Returns:
        Restored row as dict, or None when ID is not found.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE applications
                SET is_archived = FALSE,
                    name = COALESCE(%s, name),
                    base_url = COALESCE(%s, base_url),
                    owner = COALESCE(%s, owner)
                WHERE id = %s
                RETURNING id, name, base_url, requires_sso_gateway, normalized_url, owner,
                          is_archived, created_at
                """,
                (name, base_url, owner, application_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
            logger.info("Restored archived application id=%s", application_id)
            return result


def find_by_id(application_id: int) -> Optional[dict]:
    """
    Fetches one application by primary key.

    Args:
        application_id: The application's database ID.

    Returns:
        A dict with all application fields, or None if not found.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                  SELECT id, name, base_url, requires_sso_gateway, normalized_url, owner,
                       is_archived, created_at
                FROM applications
                WHERE id = %s
                """,
                (application_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def create(name: str, base_url: Optional[str], normalized_url: Optional[str],
           application_type: Optional[str], owner: Optional[str],
           requires_sso_gateway: bool = False) -> dict:
    """
    Inserts a new application row.

    Args:
        name: Display name, required.
        base_url: Raw URL as provided by the user, optional.
        normalized_url: Normalised form of the URL, optional.
        application_type: e.g. "web", "enterprise_erp", optional.
        owner: Name of the person responsible, optional.

    Returns:
        The newly created application as a dict.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO applications (name, base_url, requires_sso_gateway, normalized_url, owner)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, name, base_url, requires_sso_gateway, normalized_url, owner,
                          is_archived, created_at
                """,
                (name, base_url, requires_sso_gateway, normalized_url, owner),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
            logger.info("Created application id=%s name=%s", result["id"], name)
            return result


def list_all(include_archived: bool = False) -> list[dict]:
    """
    Returns all applications, ordered by name.

    Args:
        include_archived: When True, includes archived applications.
            Defaults to False.

    Returns:
        A list of application dicts.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            where = "" if include_archived else "WHERE is_archived = FALSE"
            cur.execute(
                f"""
                SELECT id, name, base_url, requires_sso_gateway, normalized_url, owner,
                       is_archived, created_at
                FROM applications
                {where}
                ORDER BY name
                """
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def set_requires_sso_gateway(application_id: int, requires_sso_gateway: bool) -> Optional[dict]:
    """Sets whether this application should enforce SSO prechecks."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE applications
                SET requires_sso_gateway = %s
                WHERE id = %s
                RETURNING id, name, base_url, requires_sso_gateway, normalized_url, owner,
                          is_archived, created_at
                """,
                (requires_sso_gateway, application_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
