"""
service/jira_sync.py

Stage 3 — Jira change-detection (the scheduled poll's engine).

Answers one question on demand: which Jira requirements changed since we last
looked, and which are new? Then FLAGS the affected test cases for human
review. It never regenerates or edits a test case — flagging is the whole job.
A human decides what to do with a flag. (GxP: no unreviewed AI change to a
validated artefact.)

One pass does:
  1. Read the high-water mark for this application+project from jira_sync_state
     (NULL the first time).
  2. Ask Jira for issues updated since then (all issues, the first time).
  3. For each changed issue KEY, find test cases whose requirement_id is that
     key and whose baseline (source_updated_at, or created_at as a fallback)
     is older than the issue's new `updated` — mark them
     source_sync_status='requirement_changed'.
  4. Spot NEW requirements: issue keys present in Jira that no test case
     references yet — reported, not flagged (there is nothing to flag).
  5. Advance the high-water mark to now.

Outbound-only (we call Jira; Jira never calls us), so it is safe behind a
corporate firewall — no inbound endpoint, unlike webhooks.

Deletion handling (a stored key that has left the project) is reserved for a
later iteration; v1 covers changed + new, per the agreed scope.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from core.db.connection import get_conn
from service import jira_adapter

logger = logging.getLogger(__name__)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp (Jira's or ours) to an aware datetime, or None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    # psycopg may already hand us a datetime elsewhere; here we only get strings.
    return None


def _get_sync_cursor(application_id: int, project_key: str) -> Optional[str]:
    """The stored last_synced_at (ISO string) for this app+project, or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT last_synced_at FROM jira_sync_state
                   WHERE application_id = %s AND project_key = %s""",
                (application_id, project_key),
            )
            row = cur.fetchone()
    if row and row[0]:
        return row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])
    return None


def _set_sync_cursor(application_id: int, project_key: str, when: datetime) -> None:
    """Advance (or seed) the high-water mark for this app+project."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO jira_sync_state (application_id, project_key, last_synced_at, updated_at)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (application_id, project_key)
                   DO UPDATE SET last_synced_at = EXCLUDED.last_synced_at, updated_at = now()""",
                (application_id, project_key, when),
            )
        conn.commit()


def _flag_changed_cases(application_id: int, key: str, issue_updated: datetime) -> list[dict]:
    """
    Mark test cases linked to `key` as requirement_changed when the issue was
    updated after the test's baseline. Returns the rows that were newly flagged.

    Baseline = COALESCE(source_updated_at, created_at): the precise Jira-clock
    timestamp when we have it, the row's own creation time as a fallback.
    Only rows still 'current' are flagged, so re-running the poll is idempotent
    and does not re-flag what a human may have already actioned.
    """
    flagged = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE test_case_proposals
                SET source_sync_status = 'requirement_changed', updated_at = now()
                WHERE application_id = %s
                  AND origin = 'jira'
                  AND tags->>'requirement_id' = %s
                  AND status != 'archived'
                  AND source_sync_status = 'current'
                  AND COALESCE(source_updated_at, created_at) < %s
                RETURNING id, title
                """,
                (application_id, key, issue_updated),
            )
            cols = [d[0] for d in cur.description]
            flagged = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.commit()
    return flagged


def _known_keys(application_id: int) -> set:
    """Jira keys already referenced by this application's test cases."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT tags->>'requirement_id' FROM test_case_proposals
                   WHERE application_id = %s AND origin = 'jira'
                     AND tags->>'requirement_id' IS NOT NULL AND status != 'archived'""",
                (application_id,),
            )
            return {r[0] for r in cur.fetchall() if r[0]}


def sync_application(application_id: int,
                     project_key: Optional[str] = None,
                     issue_type: Optional[str] = None) -> dict:
    """
    Run one change-detection pass for an application against its Jira project.

    Returns a summary:
        {
          "project_key": "KAN",
          "since": "<last sync or null>",
          "changed_requirements": ["KAN-4"],       # issues that changed
          "flagged_cases": [{"id":.., "title":..}], # test cases newly flagged
          "new_requirements": ["KAN-6"],            # in Jira, no tests yet
          "checked_at": "<now>"
        }
    """
    cfg = jira_adapter.load_config(project_key=project_key, issue_type=issue_type)
    since = _get_sync_cursor(application_id, cfg.project_key)
    started = datetime.now(timezone.utc)

    logger.info("Jira sync: app=%s project=%s since=%s",
                application_id, cfg.project_key, since or "(never)")

    # Issues that changed since the high-water mark (all, the first time).
    changed = jira_adapter.fetch_requirements_updated_since(cfg, since)

    changed_requirements: list[str] = []
    flagged_cases: list[dict] = []
    for req in changed:
        if not req.key:
            continue
        issue_updated = _parse_iso(req.updated) or started
        rows = _flag_changed_cases(application_id, req.key, issue_updated)
        if rows:
            changed_requirements.append(req.key)
            flagged_cases.extend(rows)

    # New requirements: everything currently in the project that we have no
    # test cases for. (Not flagged — there is nothing to flag; surfaced so a
    # human can generate tests for them.)
    current_keys = jira_adapter.fetch_current_keys(cfg)
    known = _known_keys(application_id)
    new_requirements = sorted(current_keys - known)

    # Advance the cursor last, so a mid-run failure re-checks rather than skips.
    _set_sync_cursor(application_id, cfg.project_key, started)

    summary = {
        "project_key": cfg.project_key,
        "since": since,
        "changed_requirements": sorted(set(changed_requirements)),
        "flagged_cases": flagged_cases,
        "new_requirements": new_requirements,
        "checked_at": started.isoformat(),
    }
    logger.info("Jira sync done: app=%s changed=%d flagged=%d new=%d",
                application_id, len(summary["changed_requirements"]),
                len(flagged_cases), len(new_requirements))
    return summary


# ── Review backlog (the standing state, not a single check's delta) ─────────
#
# sync_application answers "what changed since last check". These two answer the
# questions a review screen needs: "what is STILL awaiting a human's attention"
# and "a human has now attended to this one". A flag set by the sync persists
# until mark_reviewed clears it — nothing a person has not actioned disappears.


def list_pending(application_id: int,
                 project_key: Optional[str] = None) -> dict:
    """
    The standing Jira review backlog for an application.

    Unlike sync_application (which returns one check's DELTA), this returns the
    CURRENT state: every test case still flagged 'requirement_changed' (grouped
    by its Jira requirement), plus Jira requirements that still have no tests.
    Because it reads the flag from our own DB, a changed requirement stays
    visible here across many checks until a human clears it — which is the fix
    for a change appearing once and then vanishing.

    Jira is queried best-effort, only to add requirement titles and the current
    new/uncovered list. If Jira is unreachable the changed backlog is still
    returned from the DB (titles simply absent, new list empty), so the review
    screen never goes blank because of a transient Jira outage.

    Args:
        application_id: Whose Jira-sourced tests to report.
        project_key: Jira project; defaults to the configured project.

    Returns:
        {
          "application_id": int,
          "changed": [ {requirement_id, summary, cases:[{id,title,priority}]} ],
          "new":     [ {requirement_id, summary} ],
          "changed_count": int,   # flagged test cases awaiting review
          "new_count": int,       # Jira requirements with no tests yet
          "jira_error": str | None,
        }
    """
    # 1. Standing backlog of flagged cases — pure DB, always available.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, tags->>'requirement_id' AS requirement_id,
                          priority, updated_at
                   FROM test_case_proposals
                   WHERE application_id = %s AND origin = 'jira'
                     AND source_sync_status = 'requirement_changed'
                     AND status != 'archived'
                   ORDER BY tags->>'requirement_id', id""",
                (application_id,),
            )
            cols = [d[0] for d in cur.description]
            flagged = [dict(zip(cols, r)) for r in cur.fetchall()]

    # 2. Best-effort Jira lookup: titles for those keys + the current new list.
    title_map: dict = {}
    new_keys: list = []
    jira_error = None
    try:
        cfg = jira_adapter.load_config(project_key=project_key)
        reqs = jira_adapter.fetch_requirements(cfg)
        title_map = {r.key: r.summary for r in reqs if r.key}
        current_keys = {r.key for r in reqs if r.key}
        known = _known_keys(application_id)
        new_keys = sorted(current_keys - known)
    except Exception as e:
        jira_error = str(e)
        logger.warning("list_pending: Jira lookup failed (DB backlog still returned): %s", e)

    # 3. Group the flagged cases under their requirement.
    groups: dict = {}
    for row in flagged:
        key = row.get("requirement_id") or "(unlinked)"
        g = groups.setdefault(key, {
            "requirement_id": key,
            "summary": title_map.get(key),
            "cases": [],
        })
        g["cases"].append({
            "id": row["id"],
            "title": row["title"],
            "priority": row.get("priority"),
        })

    changed = sorted(groups.values(), key=lambda g: g["requirement_id"])
    new = [{"requirement_id": k, "summary": title_map.get(k)} for k in new_keys]

    return {
        "application_id": application_id,
        "changed": changed,
        "new": new,
        "changed_count": len(flagged),
        "new_count": len(new),
        "jira_error": jira_error,
    }


def mark_reviewed(proposal_id: int) -> dict:
    """
    Clear a test case's 'requirement_changed' flag — the human action the
    detection loop waits for.

    Sets source_sync_status back to 'current' so the case leaves the review
    backlog. This is the ONLY place the flag is cleared: a sync sets it, a
    person clears it. Idempotent — only a currently-flagged row is touched, so
    a double click or a stale button is harmless.

    Args:
        proposal_id: The flagged test case (test_case_proposals.id).

    Returns:
        {"reviewed": bool, "id": int, "title": str | None, "message"?: str}
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE test_case_proposals
                   SET source_sync_status = 'current', updated_at = now()
                   WHERE id = %s AND source_sync_status = 'requirement_changed'
                   RETURNING id, title""",
                (proposal_id,),
            )
            row = cur.fetchone()
        conn.commit()
    if not row:
        return {"reviewed": False, "id": proposal_id,
                "message": "Not found, or it was not flagged as changed."}
    logger.info("Cleared requirement_changed flag on test case %s (%s)", row[0], row[1])
    return {"reviewed": True, "id": row[0], "title": row[1]}