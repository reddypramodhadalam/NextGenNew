"""
api/routes/proposals.py

GET   /v1/applications/{id}/proposals  — list proposals for an application
PATCH /v1/proposals/{id}/priority      — set priority after review
PATCH /v1/proposals/{id}/status        — update status
PATCH /v1/proposals/{id}/steps         — edit the steps of an accepted test case
POST  /v1/proposals/{id}/archive       — archive a proposal
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.db.queries import proposals as proposals_db

logger = logging.getLogger(__name__)
router = APIRouter()


class PriorityRequest(BaseModel):
    priority: str  # P0, P1, P2, P3, P4


class StatusRequest(BaseModel):
    status: str


class StepInput(BaseModel):
    step: str
    expected: Optional[str] = None
    matched: bool = True
    element_hint: Optional[str] = None
    # Preserved so re-saving steps from the review UI (even when only
    # unrelated steps changed) doesn't silently strip data the survey or
    # the deterministic trace compiler already produced. See the matching
    # fields on api.routes.run.AcceptedStep.
    data_input: Optional[str] = None
    wait_condition: Optional[str] = None
    action: Optional[str] = None


class StepsRequest(BaseModel):
    steps: list[StepInput]
    edited_by: Optional[str] = None


@router.get("/applications/{application_id}/proposals")
def list_proposals(application_id: int, status: Optional[str] = None):
    """
    Returns all proposals for an application, optionally filtered by status.

    Each proposal is enriched with script_count and run_count so the UI can
    show at a glance which test cases actually have automation behind them.
    Without this, a scenario with a working script looked identical to an
    empty one until you expanded it.
    """
    logger.info("Listing proposals app_id=%s status=%s", application_id, status)
    proposals = proposals_db.list_for_application(application_id, status_filter=status)
    if not proposals:
        logger.info("No proposals found for app_id=%s status=%s", application_id, status)
        return proposals

    from core.db.connection import get_conn
    counts = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.id,
                       -- Retired versions are kept as a record but must not be
                       -- counted as scripts you have: the list hides them, so a
                       -- count that includes them reads as a contradiction
                       -- ("3 scripts" above a single row).
                       COUNT(DISTINCT g.id) FILTER (
                           WHERE g.script_path IS NOT NULL AND g.is_stale = FALSE
                       ) AS scripts,
                       COUNT(DISTINCT g.id) FILTER (
                           WHERE g.script_path IS NOT NULL
                             AND g.is_stale = FALSE
                             AND g.generation_status = 'success'
                       ) AS success_scripts,
                       COUNT(DISTINCT g.id) FILTER (
                           WHERE g.script_path IS NOT NULL
                             AND g.is_stale = FALSE
                             AND g.generation_status = 'incomplete'
                       ) AS incomplete_scripts,
                       -- Runs DO include those from retired scripts. They
                       -- happened, and that history is the audit evidence.
                       COUNT(e.id) AS runs,
                       -- The result of this test case's MOST RECENT run, so the
                       -- UI can show a "failing" count and a per-row status dot.
                       -- 'pass' | 'fail' | 'skip' | 'error', or NULL if never run.
                       (SELECT e2.result
                          FROM executions e2
                          JOIN generations g2 ON g2.id = e2.generation_id
                         WHERE g2.test_case_proposal_id = p.id
                         ORDER BY e2.executed_at DESC
                                                 LIMIT 1) AS last_result,
                                             (SELECT g3.generation_status
                                                    FROM generations g3
                                                 WHERE g3.test_case_proposal_id = p.id
                                                     AND g3.is_stale = FALSE
                                                 ORDER BY g3.completed_at DESC NULLS LAST, g3.id DESC
                                                 LIMIT 1) AS latest_generation_status,
                                             (SELECT g4.notes
                                                    FROM generations g4
                                                 WHERE g4.test_case_proposal_id = p.id
                                                     AND g4.is_stale = FALSE
                                                 ORDER BY g4.completed_at DESC NULLS LAST, g4.id DESC
                                                 LIMIT 1) AS latest_generation_notes
                FROM test_case_proposals p
                LEFT JOIN generations g ON g.test_case_proposal_id = p.id
                LEFT JOIN executions  e ON e.generation_id = g.id
                WHERE p.application_id = %s
                GROUP BY p.id
                """,
                (application_id,),
            )
            counts = {
                r[0]: {
                    "script_count": r[1],
                    "success_script_count": r[2],
                    "incomplete_script_count": r[3],
                    "run_count": r[4],
                    "last_result": r[5],
                    "latest_generation_status": r[6],
                    "latest_generation_notes": r[7],
                }
                for r in cur.fetchall()
            }

    for p in proposals:
        info = counts.get(p["id"], {})
        p["script_count"] = info.get("script_count", 0)
        p["success_script_count"] = info.get("success_script_count", 0)
        p["incomplete_script_count"] = info.get("incomplete_script_count", 0)
        p["run_count"] = info.get("run_count", 0)
        p["last_result"] = info.get("last_result")  # 'pass'|'fail'|'skip'|'error'|None
        p["latest_generation_status"] = info.get("latest_generation_status")
        p["latest_generation_notes"] = info.get("latest_generation_notes")
    logger.info("Listed %s proposals for app_id=%s", len(proposals), application_id)
    return proposals


@router.patch("/proposals/{proposal_id}/priority")
def set_priority(proposal_id: int, body: PriorityRequest):
    """Sets the priority level (P0–P4) after human review."""
    valid = {"P0", "P1", "P2", "P3", "P4"}
    if body.priority not in valid:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PRIORITY", "message": f"Priority must be one of {valid}."})
    proposals_db.update_priority(proposal_id, body.priority)
    return {"ok": True}


@router.patch("/proposals/{proposal_id}/status")
def update_status(proposal_id: int, body: StatusRequest):
    """Updates a proposal's status."""
    proposals_db.update_status(proposal_id, body.status)
    return {"ok": True}


@router.patch("/proposals/{proposal_id}/steps")
def update_steps(proposal_id: int, body: StepsRequest):
    """
    Replace the steps of an accepted test case.

    An accepted test case is the audit artefact — it is what a person
    approved. Editing its steps afterwards is a recorded event, not a silent
    update: the previous steps are written to proposal_edits along with who
    made the change.

    The description is re-derived from the steps in the same transaction.
    Callers cannot set it independently; it is a projection of the steps, and
    letting the two drift would mean generation reads something the reviewer
    never approved.

    Args:
        proposal_id: The test case to edit.
        body: The new steps, in order, and who is making the change.

    Returns:
        {"ok": True, "proposal": {...}, "step_count": int}

    Raises:
        400: If every step is blank — a test case with no steps should be
            edited to have steps, or archived, not silently emptied.
        404: If the proposal does not exist.
    """
    from service.test_case_import import flatten_steps

    steps = [s.model_dump() for s in body.steps]
    steps = [s for s in steps if (s.get("step") or "").strip()]
    if not steps:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "NO_STEPS",
                "message": "At least one step with an action is required.",
            },
        )

    # Numbered here, from array order — never taken from the client, so
    # reordering in the UI cannot produce gaps or duplicate numbers.
    for i, s in enumerate(steps, 1):
        s["n"] = i

    updated = proposals_db.update_steps(
        proposal_id=proposal_id,
        steps=steps,
        description=flatten_steps(steps),
        edited_by=body.edited_by,
    )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": f"Test case {proposal_id} not found."},
        )

    return {"ok": True, "proposal": updated, "step_count": len(steps)}


@router.post("/proposals/{proposal_id}/archive")
def archive_proposal(proposal_id: int):
    """Archives a proposal — reversible, stops it counting in coverage."""
    proposals_db.update_status(proposal_id, "archived")
    return {"ok": True}