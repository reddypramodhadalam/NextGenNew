"""
api/routes/executions.py

GET /v1/generations/{id}/executions  — execution history for a generation
"""

import logging
from fastapi import APIRouter
from core.db.queries import executions as executions_db

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/generations/{generation_id}/executions")
def list_executions(generation_id: int):
    """Returns execution history for a generation, newest first."""
    return executions_db.list_for_generation(generation_id)


# ── Execution history across the whole estate ────────────────────────────
#
# Appended rather than rewritten: the endpoint above is in use and there is
# no reason to touch it.

from typing import Optional                      # noqa: E402
from core.db.connection import get_conn          # noqa: E402


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/executions")
def list_all_executions(
    application_id: Optional[int] = None,
    result: Optional[str] = None,
    triggered_by: Optional[str] = None,
    limit: int = 100,
):
    """
    Every execution, newest first, with the context needed to make sense of
    it — which test, which application, which environment, and whether a
    person or the scheduler started it.

    This is the single place run history lives. Other screens link here
    rather than each rendering their own version of it.
    """
    where, params = [], []
    if application_id:
        where.append("p.application_id = %s"); params.append(application_id)
    if result:
        where.append("e.result = %s"); params.append(result)
    if triggered_by:
        where.append("e.triggered_by = %s"); params.append(triggered_by)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT e.id, e.result, e.executed_at, e.duration_ms,
                           e.triggered_by, e.environment, e.screenshot_path,
                           p.title AS test_title, p.priority, p.category,
                           -- The scenario a test belongs to, so a run can be
                           -- shown in context without repeating it per row.
                           p.tags->>'scenario' AS scenario,
                           -- The URL behind the environment name, so the UI can
                           -- show WHERE a run happened on hover rather than just
                           -- a bare label like "default".
                           (SELECT ae.base_url FROM application_environments ae
                             WHERE ae.application_id = p.application_id
                               AND lower(ae.name) = lower(COALESCE(e.environment, ''))
                             LIMIT 1) AS environment_url,
                           a.name AS application_name, a.id AS application_id,
                           g.id AS generation_id
                    FROM executions e
                    JOIN generations g ON g.id = e.generation_id
                    JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                    JOIN applications a ON a.id = p.application_id
                    {clause}
                    ORDER BY e.executed_at DESC
                    LIMIT %s""",
                tuple(params),
            )
            return {"executions": _rows(cur)}


@router.get("/executions/{execution_id}")
def execution_detail(execution_id: int):
    """
    One execution in full: the result, why it failed, and its screenshots.

    Reuses the same screenshot listing as the per-test history so the two
    can never show different evidence for the same run.
    """
    import os
    from pathlib import Path

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.*, p.title AS test_title, p.priority, p.category,
                          p.description, a.name AS application_name,
                          a.id AS application_id, g.script_path
                   FROM executions e
                   JOIN generations g ON g.id = e.generation_id
                   JOIN test_case_proposals p ON p.id = g.test_case_proposal_id
                   JOIN applications a ON a.id = p.application_id
                   WHERE e.id = %s""",
                (execution_id,),
            )
            rows = _rows(cur)

    if not rows:
        return {"error": "Execution not found."}
    ex = rows[0]

    shots = []
    folder = ex.get("screenshot_path")
    if folder:
        try:
            d = Path(folder)
            if d.exists():
                run_id = d.name
                shots = [f"/v1/screenshots/{run_id}/{f.name}"
                         for f in sorted(d.glob("*.png"))]
        except OSError:
            pass
    ex["screenshots"] = shots
    return {"execution": ex}


@router.get("/schedule-runs")
def list_schedule_runs(limit: int = 50):
    """
    Suite runs started by a schedule or by "Run now".

    Separate from individual executions because a suite run is one event
    covering many tests — showing its members as loose rows would lose that.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.*, s.name AS schedule_name, s.frequency,
                          ts.name AS suite_name, ts.kind AS suite_kind,
                          a.name AS application_name, a.id AS application_id
                   FROM schedule_runs r
                   JOIN schedules s ON s.id = r.schedule_id
                   LEFT JOIN test_suites ts ON ts.id = s.suite_id
                   JOIN applications a ON a.id = s.application_id
                   ORDER BY r.started_at DESC LIMIT %s""",
                (limit,),
            )
            return {"runs": _rows(cur)}
