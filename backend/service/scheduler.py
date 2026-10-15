"""
service/scheduler.py

Runs scheduled jobs (test suites and Jira change-detection) on one clock.

A single background task inside the backend: it wakes every 30 seconds, finds
schedules whose next run time has passed, and executes them. No extra process
to start, and nothing to install.

Two design points that matter:

  1. A scheduled run and the "Run now" button use the SAME code path
     (run_schedule below). What you demonstrate by hand is exactly what fires
     at 6am — there is no separate, untested path that only runs when nobody
     is watching.

  2. This runner owns the LIFECYCLE only — record the run, notify, work out
     the next fire time — and never knows WHAT a schedule does. The actual work
     is delegated to a job (service/scheduler_jobs.py) chosen by the schedule's
     job_type. Adding a new kind of scheduled work is a new job class there,
     not an edit here.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.db.connection import get_conn
from service.scheduler_jobs import get_job

logger = logging.getLogger(__name__)

TICK_SECONDS = 30      # how often to look for due work
_task: Optional[asyncio.Task] = None


def compute_next_run(frequency: str, current: datetime) -> Optional[datetime]:
    """
    Work out when a schedule should next fire.

    Args:
        frequency: once | hourly | daily | weekdays | weekly.
        current: The occurrence that has just run.

    Returns:
        The next occurrence, or None for 'once' (it does not repeat).
    """
    if frequency == "once":
        return None
    if frequency == "hourly":
        return current + timedelta(hours=1)
    if frequency == "weekly":
        return current + timedelta(weeks=1)

    nxt = current + timedelta(days=1)
    if frequency == "weekdays":
        while nxt.weekday() >= 5:          # skip Saturday and Sunday
            nxt += timedelta(days=1)
    return nxt


def _post_webhook(url: str, payload: dict) -> str:
    """
    Send the run summary to a webhook (n8n, which then emails).

    NextGen2 never holds mail credentials — it reports what happened and the
    integration layer decides how to deliver it. That keeps the channel
    swappable (email today, Teams tomorrow) and keeps another secret out of
    this application.

    Returns:
        A short status string recorded against the run.
    """
    if not url:
        return "no webhook configured"
    try:
        import requests
        r = requests.post(url, json=payload, timeout=20)
        if 200 <= r.status_code < 300:
            logger.info("Webhook accepted the run summary (%s)", r.status_code)
            return f"sent ({r.status_code})"
        logger.warning("Webhook returned %s", r.status_code)
        return f"failed ({r.status_code})"
    except Exception as e:
        logger.warning("Could not reach the webhook: %s", e)
        return f"error: {e}"[:200]


def run_schedule(schedule_id: int, triggered_by: str = "scheduler") -> dict:
    """
    Execute a schedule and record the outcome.

    Used by both the background tick and the "Run now" button. The runner does
    not decide what the schedule does — it loads the row, picks the matching
    job from the registry (by job_type), validates, runs it, then persists and
    notifies. Every job type flows through this one path identically.

    Args:
        schedule_id: Which schedule to run.
        triggered_by: 'scheduler' or 'manual' — stored so a run can be traced
            to whether a person or the clock started it.

    Returns:
        The run summary (the job's result, augmented with run/status fields).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT s.name, s.suite_id, s.environment, s.webhook_url,
                          s.frequency, s.next_run_at, s.run_at,
                          ts.name AS suite_name, a.name AS app_name, s.application_id,
                          s.job_type, s.jira_project_key
                   FROM schedules s
                   LEFT JOIN test_suites ts ON ts.id = s.suite_id
                   JOIN applications a ON a.id = s.application_id
                   WHERE s.id = %s""",
                (schedule_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"error": "Schedule not found."}
            (name, suite_id, environment, webhook_url, frequency,
             next_run_at, run_at, suite_name, app_name, application_id,
             job_type, jira_project_key) = row

            # One dispatch point: pick the job for this schedule's type. No
            # per-type branching in the runner — a new job type is a new class
            # in scheduler_jobs.py, not an edit here.
            job = get_job(job_type)
            if job is None:
                return {"error": f"Unknown schedule job_type '{job_type}'."}

            schedule = {
                "suite_id": suite_id,
                "application_id": application_id,
                "environment": environment,
                "jira_project_key": jira_project_key,
            }
            problem = job.validate(schedule)
            if problem:
                return {"error": problem}

            cur.execute(
                "INSERT INTO schedule_runs (schedule_id, triggered_by) VALUES (%s, %s) RETURNING id",
                (schedule_id, triggered_by),
            )
            run_id = cur.fetchone()[0]
        conn.commit()

    logger.info("Schedule '%s' starting: %s on %s [%s]",
                name, job_type, app_name, environment or "default")

    # Delegate the actual work to the job. The runner stays lifecycle-only and
    # never knows whether this ran a suite or a Jira sync.
    result = job.run(schedule, triggered_by)
    status = result.status
    summary = result.summary

    payload = {
        "event": result.event,
        "schedule": name,
        "triggered_by": triggered_by,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        **summary,
    }
    webhook_status = _post_webhook(webhook_url, payload)

    following = compute_next_run(frequency, next_run_at or run_at or datetime.now(timezone.utc))

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Counters are suite-semantics; a Jira sync returns none, so these
            # default to 0 (the sync's real detail is in the summary column).
            cur.execute(
                """UPDATE schedule_runs
                   SET finished_at = now(), total = %s, passed = %s, failed = %s,
                       skipped = %s, summary = %s, webhook_status = %s
                   WHERE id = %s""",
                (result.counters.get("total", 0), result.counters.get("passed", 0),
                 result.counters.get("failed", 0), result.counters.get("skipped", 0),
                 json.dumps(payload, default=str), webhook_status, run_id),
            )
            cur.execute(
                """UPDATE schedules
                   SET last_run_at = now(), last_status = %s, last_summary = %s,
                       next_run_at = %s,
                       enabled = CASE WHEN %s IS NULL THEN FALSE ELSE enabled END,
                       updated_at = now()
                   WHERE id = %s""",
                (status, json.dumps(payload, default=str), following, following, schedule_id),
            )
        conn.commit()

    logger.info("Schedule '%s' finished: %s · webhook: %s",
                name, status, webhook_status)

    summary["schedule_run_id"] = run_id
    summary["status"] = status
    summary["webhook_status"] = webhook_status
    summary["next_run_at"] = following.isoformat() if following else None
    return summary


def _due_schedules() -> list[int]:
    """Schedules that are enabled and whose next run time has passed."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM schedules
                   WHERE enabled = TRUE
                     AND next_run_at IS NOT NULL
                     AND next_run_at <= now()
                   ORDER BY next_run_at""",
            )
            return [r[0] for r in cur.fetchall()]


async def _loop():
    """Wake regularly, run whatever is due, never die on an error."""
    logger.info("Scheduler started (checking every %ds)", TICK_SECONDS)
    while True:
        try:
            due = await asyncio.to_thread(_due_schedules)
            for sid in due:
                logger.info("Schedule %s is due", sid)
                # to_thread: the job may block (Selenium, network I/O), and must
                # not stall the event loop serving the UI.
                await asyncio.to_thread(run_schedule, sid, "scheduler")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failure in one tick must never stop the scheduler.
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


def start():
    """Start the background scheduler. Called once on application startup."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())


def stop():
    """Stop the scheduler on shutdown."""
    global _task
    if _task:
        _task.cancel()
        _task = None
        logger.info("Scheduler stopped")
