"""
api/routes/schedules.py

Scheduled runs — test suites and Jira change-detection.

  GET    /v1/schedules
  POST   /v1/schedules
  PATCH  /v1/schedules/{id}
  DELETE /v1/schedules/{id}
  POST   /v1/schedules/{id}/run          run it now
  GET    /v1/schedules/{id}/runs         its run history

A schedule's job_type decides what running it does. 'suite_run' executes a
test suite (the original behaviour); 'jira_sync' runs one Jira change-detection
pass. Both ride the same runner and the same "Run now" path — the route layer
only differs in what a create request must supply.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.db.connection import get_conn
from service import scheduler as scheduler_service

logger = logging.getLogger(__name__)
router = APIRouter()


class ScheduleIn(BaseModel):
    application_id: int
    name: str
    job_type: str = "suite_run"                 # 'suite_run' | 'jira_sync'
    suite_id: Optional[int] = None              # required for suite_run
    jira_project_key: Optional[str] = None      # required for jira_sync
    frequency: str = "daily"
    run_at: str                                 # ISO datetime of the first occurrence
    environment: Optional[str] = None
    webhook_url: Optional[str] = None
    recipients: Optional[str] = None
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: Optional[str] = None
    frequency: Optional[str] = None
    run_at: Optional[str] = None
    environment: Optional[str] = None
    webhook_url: Optional[str] = None
    recipients: Optional[str] = None
    enabled: Optional[bool] = None


def _rows(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


@router.get("/schedules/summary")
def schedules_summary():
    """
    What is due next, and how the last automated run went.

    For the dashboard. The question a team asks first each morning is "did
    last night's run pass, and when do I next get fresh data" — and if
    nothing is scheduled at all, that absence is itself worth showing.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.id, s.name, s.frequency, s.next_run_at, s.environment,
                          ts.name AS suite_name, a.name AS application_name
                   FROM schedules s
                   LEFT JOIN test_suites ts ON ts.id = s.suite_id
                   JOIN applications a ON a.id = s.application_id
                   WHERE s.enabled = TRUE AND s.next_run_at IS NOT NULL
                   ORDER BY s.next_run_at LIMIT 3""",
            )
            upcoming = _rows(cur)

            cur.execute(
                """SELECT r.id, r.started_at, r.total, r.passed, r.failed, r.skipped,
                          r.triggered_by, s.name AS schedule_name,
                          ts.name AS suite_name, a.name AS application_name
                   FROM schedule_runs r
                   JOIN schedules s ON s.id = r.schedule_id
                   LEFT JOIN test_suites ts ON ts.id = s.suite_id
                   JOIN applications a ON a.id = s.application_id
                   WHERE r.finished_at IS NOT NULL
                   ORDER BY r.started_at DESC LIMIT 3""",
            )
            recent = _rows(cur)

            cur.execute("SELECT COUNT(*) FROM schedules")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM schedules WHERE enabled = TRUE")
            active = cur.fetchone()[0]

    return {
        "upcoming": upcoming,
        "recent": recent,
        "total": total,
        "active": active,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/schedules")
def list_schedules(application_id: Optional[int] = None):
    """
    All schedules, newest first, with enough context to render the list
    without a second call per row.

    s.* carries job_type and jira_project_key, so the UI can tell a suite
    schedule from a Jira one and show the right detail. member_count is a
    suite notion; for a Jira schedule (no suite) it comes back 0 and the UI
    simply does not show it.
    """
    where, params = "", []
    if application_id:
        where = "WHERE s.application_id = %s"
        params.append(application_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT s.*, a.name AS application_name, ts.name AS suite_name,
                           ts.kind AS suite_kind,
                           (SELECT COUNT(*) FROM test_suite_members m
                             WHERE m.suite_id = s.suite_id) AS member_count
                    FROM schedules s
                    JOIN applications a ON a.id = s.application_id
                    LEFT JOIN test_suites ts ON ts.id = s.suite_id
                    {where}
                    ORDER BY s.enabled DESC, s.next_run_at NULLS LAST, s.created_at DESC""",
                tuple(params),
            )
            schedules = _rows(cur)

            for s in schedules:
                cur.execute(
                    """SELECT passed, failed, skipped, started_at
                       FROM schedule_runs WHERE schedule_id = %s
                       ORDER BY started_at DESC LIMIT 7""",
                    (s["id"],),
                )
                s["recent_runs"] = _rows(cur)
    return {"schedules": schedules, "server_time": datetime.now(timezone.utc).isoformat()}


@router.post("/schedules")
def create_schedule(body: ScheduleIn, request: Request):
    """
    Create a schedule.

    run_at is the first occurrence, and is also stored as next_run_at so the
    UI can count down to it and the scheduler can find it with one query.

    What a create must supply depends on job_type:
      suite_run  → a suite_id (which suite to run).
      jira_sync  → a jira_project_key (which project to check). No suite.
    Everything else (cadence, run_at, webhook, recipients) is shared.
    """
    who = request.headers.get("X-Triggered-By", "anonymous")

    job_type = (body.job_type or "suite_run").strip()
    if job_type not in ("suite_run", "jira_sync"):
        return {"error": f"Unknown job_type '{job_type}'."}

    # Type-specific requirements — mirror the runner's own validate() so a bad
    # schedule is rejected at creation, not discovered at run time.
    if job_type == "suite_run" and not body.suite_id:
        return {"error": "A suite is required for a suite-run schedule."}
    project_key = (body.jira_project_key or "").strip() or None
    if job_type == "jira_sync" and not project_key:
        return {"error": "A Jira project is required for a Jira-sync schedule."}

    # A Jira schedule has no suite; store NULL rather than whatever was sent.
    suite_id = body.suite_id if job_type == "suite_run" else None

    try:
        run_at = datetime.fromisoformat(body.run_at.replace("Z", "+00:00"))
    except ValueError:
        return {"error": "run_at must be an ISO datetime."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO schedules
                   (application_id, suite_id, name, frequency, run_at, next_run_at,
                    environment, webhook_url, recipients, enabled, created_by,
                    job_type, jira_project_key)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (body.application_id, suite_id, body.name.strip(), body.frequency,
                 run_at, run_at, body.environment, body.webhook_url, body.recipients,
                 body.enabled, who, job_type, project_key),
            )
            sched = _rows(cur)[0]
        conn.commit()
    logger.info("Created %s schedule '%s' (%s) first run %s",
                job_type, body.name, body.frequency, run_at)
    return {"schedule": sched}


@router.patch("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, body: SchedulePatch):
    """Update a schedule. Changing run_at also moves the next occurrence."""
    sets, vals = [], []
    for field in ("name", "frequency", "environment", "webhook_url", "recipients"):
        v = getattr(body, field)
        if v is not None:
            sets.append(f"{field} = %s"); vals.append(v)
    if body.enabled is not None:
        sets.append("enabled = %s"); vals.append(body.enabled)
    if body.run_at:
        try:
            dt = datetime.fromisoformat(body.run_at.replace("Z", "+00:00"))
        except ValueError:
            return {"error": "run_at must be an ISO datetime."}
        sets += ["run_at = %s", "next_run_at = %s"]; vals += [dt, dt]
    if not sets:
        return {"error": "Nothing to update."}
    sets.append("updated_at = now()")
    vals.append(schedule_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE schedules SET {', '.join(sets)} WHERE id = %s RETURNING *",
                        tuple(vals))
            rows = _rows(cur)
        conn.commit()
    return {"schedule": rows[0]} if rows else {"error": "Schedule not found."}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int):
    """Delete a schedule and its run records. Test results are untouched."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM schedule_runs WHERE schedule_id = %s", (schedule_id,))
            cur.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
            deleted = cur.rowcount
        conn.commit()
    return {"deleted": bool(deleted), "schedule_id": schedule_id}


@router.post("/schedules/{schedule_id}/run")
def run_now(schedule_id: int):
    """
    Run a schedule immediately.

    Deliberately the same function the background scheduler calls, so this is
    a genuine rehearsal of the scheduled run rather than a lookalike. The
    returned dict is whatever the job produced — a suite result for a suite
    schedule, a change-detection summary for a Jira one.
    """
    return scheduler_service.run_schedule(schedule_id, triggered_by="manual")


@router.get("/schedules/{schedule_id}/runs")
def schedule_runs(schedule_id: int, limit: int = 20):
    """Recent runs for a schedule."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, started_at, finished_at, total, passed, failed, skipped,
                          triggered_by, webhook_status, summary
                   FROM schedule_runs WHERE schedule_id = %s
                   ORDER BY started_at DESC LIMIT %s""",
                (schedule_id, limit),
            )
            return {"runs": _rows(cur)}