"""
api/routes/environments.py

Manage the environments an application is tested against.

  GET    /v1/applications/{id}/environments
  POST   /v1/applications/{id}/environments
  PATCH  /v1/environments/{env_id}
  DELETE /v1/environments/{env_id}

A test case is written once and run against dev, test, stage or production,
so the URL belongs to the environment rather than the test. Keeping these in
the database (rather than a config file on the server) means whoever owns the
application can maintain them, and a URL change is an edit in the UI instead
of a deployment.
"""

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from core.db.connection import get_conn

logger = logging.getLogger(__name__)
router = APIRouter()


class EnvironmentIn(BaseModel):
    name: str
    base_url: str
    is_default: bool = False
    notes: Optional[str] = None


class EnvironmentPatch(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    is_default: Optional[bool] = None
    notes: Optional[str] = None


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/applications/{application_id}/environments")
def list_environments(application_id: int):
    """
    Return the environments configured for an application.

    If none are configured yet but the application has a base_url, a
    'default' environment is created from it so execution always has
    somewhere to point.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM application_environments
                   WHERE application_id = %s
                   ORDER BY is_default DESC, name""",
                (application_id,),
            )
            envs = _rows(cur)

            if not envs:
                cur.execute("SELECT base_url FROM applications WHERE id = %s",
                            (application_id,))
                row = cur.fetchone()
                if row and row[0]:
                    cur.execute(
                        """INSERT INTO application_environments
                           (application_id, name, base_url, is_default)
                           VALUES (%s, 'default', %s, TRUE)
                           ON CONFLICT (application_id, name) DO NOTHING
                           RETURNING *""",
                        (application_id, row[0]),
                    )
                    envs = _rows(cur)
                    conn.commit()
                    logger.info("Seeded default environment for application %s",
                                application_id)
    return {"environments": envs}


@router.post("/applications/{application_id}/environments")
def create_environment(application_id: int, body: EnvironmentIn):
    """Add an environment. Setting it as default clears the previous one."""
    name = (body.name or "").strip()
    url = (body.base_url or "").strip()
    if not name or not url:
        return {"error": "Both a name and a URL are required."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM application_environments WHERE application_id=%s AND lower(name)=lower(%s)",
                (application_id, name),
            )
            if cur.fetchone():
                return {"error": f"An environment called '{name}' already exists."}

            if body.is_default:
                cur.execute(
                    "UPDATE application_environments SET is_default = FALSE WHERE application_id = %s",
                    (application_id,),
                )
            cur.execute(
                """INSERT INTO application_environments
                   (application_id, name, base_url, is_default, notes)
                   VALUES (%s, %s, %s, %s, %s) RETURNING *""",
                (application_id, name, url, bool(body.is_default), body.notes),
            )
            env = _rows(cur)[0]
        conn.commit()
    logger.info("Created environment '%s' for application %s", name, application_id)
    return {"environment": env}


@router.patch("/environments/{env_id}")
def update_environment(env_id: int, body: EnvironmentPatch):
    """Update an environment — typically because its URL moved."""
    sets, vals = [], []
    if body.name is not None:
        sets.append("name = %s"); vals.append(body.name.strip())
    if body.base_url is not None:
        sets.append("base_url = %s"); vals.append(body.base_url.strip())
    if body.notes is not None:
        sets.append("notes = %s"); vals.append(body.notes)
    if not sets and body.is_default is None:
        return {"error": "Nothing to update."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            if body.is_default:
                cur.execute(
                    """UPDATE application_environments SET is_default = FALSE
                       WHERE application_id = (
                           SELECT application_id FROM application_environments WHERE id = %s
                       )""",
                    (env_id,),
                )
                sets.append("is_default = TRUE")
            elif body.is_default is False:
                sets.append("is_default = FALSE")

            sets.append("updated_at = now()")
            vals.append(env_id)
            cur.execute(
                f"UPDATE application_environments SET {', '.join(sets)} WHERE id = %s RETURNING *",
                tuple(vals),
            )
            rows = _rows(cur)
        conn.commit()
    if not rows:
        return {"error": "Environment not found."}
    return {"environment": rows[0]}


@router.delete("/environments/{env_id}")
def delete_environment(env_id: int):
    """
    Remove an environment.

    Executions keep the environment NAME they ran against as text, so
    deleting a configuration never rewrites history.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM application_environments WHERE id = %s", (env_id,))
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        return {"error": "Environment not found."}
    return {"deleted": True, "environment_id": env_id}


def resolve_url(application_id: int, environment: Optional[str] = None,
                override_url: Optional[str] = None) -> tuple[str, str]:
    """
    Work out which URL to run against.

    Order of precedence:
      1. an explicit URL typed for this run (ad-hoc target)
      2. the named environment
      3. the application's default environment
      4. the application's own base_url (older applications)

    Returns:
        (url, environment_label) — the label is stored on the execution so a
        result can always be traced to where it ran.
    """
    if override_url:
        return override_url.strip(), (environment or "custom")

    with get_conn() as conn:
        with conn.cursor() as cur:
            if environment:
                cur.execute(
                    """SELECT base_url, name FROM application_environments
                       WHERE application_id = %s AND lower(name) = lower(%s)""",
                    (application_id, environment),
                )
                row = cur.fetchone()
                if row:
                    return row[0], row[1]

            cur.execute(
                """SELECT base_url, name FROM application_environments
                   WHERE application_id = %s ORDER BY is_default DESC, name LIMIT 1""",
                (application_id,),
            )
            row = cur.fetchone()
            if row:
                return row[0], row[1]

            cur.execute("SELECT base_url FROM applications WHERE id = %s", (application_id,))
            row = cur.fetchone()
            return (row[0] if row else ""), "default"
