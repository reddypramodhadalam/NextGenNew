"""
service/action_resolver.py

ACTION RESOLVER

Implements the next_action decision table from the functional spec (F10).
Given a proposal's current state, staleness, and last execution result,
returns exactly one next action.

This is a deterministic lookup — not a judgment call. Every possible
situation has one correct answer already enumerated in the spec. If a
new situation arises that isn't covered, this module should be updated,
not the agents.

Also enforces the concurrency rule: locks the proposal row before any
write (SELECT ... FOR UPDATE) so a scheduled batch and a manual trigger
cannot both act on the same proposal simultaneously.
"""

import logging
from typing import Optional
from core.db.connection import get_conn

logger = logging.getLogger(__name__)

# Possible next actions as string constants
GENERATE = "generate"
REGENERATE = "regenerate"
PROVIDE_DATA = "provide_data"
RUN = "run"
RUN_AGAIN = "run_again"
FIX_IT = "fix_it"
REBUILD = "rebuild"
NONE = "none"


def get_next_action(proposal: dict, last_execution: Optional[dict]) -> str:
    """
    Computes the primary next action for a proposal.

    Args:
        proposal: A test_case_proposals row dict.
        last_execution: The most recent executions row for this proposal's
            current generation, or None if it has never been executed.

    Returns:
        One of the action constants defined above.
    """
    status = proposal.get("status", "proposed")

    # Archived overrides everything — checked first.
    if status == "archived":
        return NONE

    # Out of date overrides everything below it.
    # An out-of-date pass is not a real pass.
    if status == "stale":
        return REBUILD

    if status == "manual_only":
        return NONE

    if status in ("proposed", "queued"):
        # TODO: Check if any required step data is genuinely missing.
        # For now, assume data is always present when a proposal is queued.
        return GENERATE

    if status == "generating":
        return NONE

    if status == "generated":
        if last_execution is None:
            return RUN
        result = last_execution.get("result")
        if result == "pass":
            return RUN_AGAIN
        if result in ("fail", "error"):
            return FIX_IT
        return RUN_AGAIN

    return NONE


def lock_proposal_for_update(proposal_id: int, conn) -> Optional[dict]:
    """
    Fetches a proposal with a row-level lock (SELECT ... FOR UPDATE).
    Prevents concurrent batch and manual triggers from both acting on
    the same proposal simultaneously.

    Args:
        proposal_id: The proposal's database ID.
        conn: An active database connection (from get_conn() context manager).

    Returns:
        The locked proposal row as a dict, or None if not found.
        The lock is released when the transaction commits or rolls back.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM test_case_proposals
            WHERE id = %s
            FOR UPDATE
            """,
            (proposal_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
