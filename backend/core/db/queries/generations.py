"""
core/db/queries/generations.py

All database reads and writes for the `generations` table.

Key design decisions:
    - script_content is the source of truth, not script_path.
      The file on disk is derived from the database. If the file is
      ever lost, regenerate it from script_content — never the reverse.
    - generation_status starts as 'running' at insert time and is
      updated to 'success' or 'incomplete' when the agent finishes.
      This means a crashed/interrupted generation is detectable —
      its row stays in 'running' state indefinitely, which is honest
      and recoverable, unlike leaving the column null.
"""

import json
import logging
from typing import Optional

from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def create(
    test_case_proposal_id: int,
    provider: str,
    model: str,
    triggered_by: Optional[str] = None,
    attempt_number: int = 1,
    app_map_id: Optional[int] = None,
) -> dict:
    """
    Inserts a new generation attempt row with generation_status='running'.

    Called at the START of a generation, before the agent runs. The row
    records that work is in progress. If the process crashes after this
    point, the row stays in 'running' state — detectable as an
    interrupted generation, not silently lost.

    Args:
        test_case_proposal_id: FK to test_case_proposals.
        provider: The LLM provider name, e.g. 'gemini', 'bedrock'.
        model: The exact model identifier used, e.g. 'gemini-2.5-flash'.
        triggered_by: Who or what started this — a person's name or
            'scheduled' for an unattended run. Nullable for MVP.
        attempt_number: Which attempt this is. 1 for first, 2 for a
            retry after a previous incomplete attempt, etc.

    Returns:
        The newly created generation row as a dict, including its id
        which the orchestrator uses to call complete() afterward.

    Raises:
        psycopg2.DatabaseError: On any database write failure.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generations (
                    test_case_proposal_id,
                    provider,
                    model,
                    triggered_by,
                    attempt_number,
                    generation_status,
                    app_map_id
                )
                VALUES (%s, %s, %s, %s, %s, 'running', %s)
                RETURNING *
                """,
                (
                    test_case_proposal_id,
                    provider,
                    model,
                    triggered_by,
                    attempt_number,
                    app_map_id,
                ),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
            logger.info(
                "Started generation id=%s proposal=%s attempt=%s provider=%s",
                result["id"],
                test_case_proposal_id,
                attempt_number,
                provider,
            )
            return result


def complete(
    generation_id: int,
    generation_status: str,
    test_verdict: Optional[str],
    script_content: Optional[str],
    script_path: Optional[str],
    steps_taken: Optional[int],
    trace: Optional[dict],
    notes: Optional[str],
    *,
    replace_existing: bool = False,
) -> None:
    """
    Updates a generation row when the agent finishes — success or incomplete.

    Called at the END of a generation. Transitions generation_status from
    'running' to either 'success' or 'incomplete'.

    Args:
        generation_id: The generation's database ID, from create().
        generation_status: Final status — 'success' or 'incomplete'.
            'incomplete' means the agent gave up or an infrastructure
            error occurred. The compiled script will contain pytest.skip()
            rather than real assertions, so it never reports a false failure.
        test_verdict: 'pass', 'fail', or None. Only meaningful when
            generation_status='success'. Keeping these two columns separate
            prevents infrastructure failures from being reported as genuine
            test failures — a real bug caught during development.
        script_content: The full script text. This is the source of truth.
            script_path is derived from this, not the other way around.
        script_path: Path to the file written on disk for pytest to run.
        steps_taken: Number of agent loop iterations executed.
        trace: Full raw trace as a Python dict — stored as JSONB for
            audit and debugging. May be large.
        notes: Human-readable explanation of the outcome, especially
            useful when generation_status='incomplete'.
        replace_existing: Atomically retire prior usable scripts only after
            a successful replacement. Failed attempts preserve existing scripts.
            The parent proposal lock serializes replacements: the last successful
            completion wins, regardless of generation ID or start order.

    Returns:
        None

    Raises:
        psycopg2.DatabaseError: On any database write failure.
        ValueError: If a replacement is missing, no longer running/active,
            or has an invalid final status or missing successful script.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if replace_existing:
                # Read the parent ID without locking the child. Every replacement
                # takes the parent lock FIRST, avoiding cross-attempt deadlocks.
                cur.execute(
                    "SELECT test_case_proposal_id FROM generations WHERE id = %s",
                    (generation_id,),
                )
                candidate = cur.fetchone()
                if not candidate:
                    raise ValueError("Replacement generation does not exist")
                proposal_id = candidate[0]
                cur.execute(
                    "SELECT id FROM test_case_proposals WHERE id = %s FOR UPDATE",
                    (proposal_id,),
                )
                if not cur.fetchone():
                    raise ValueError("Replacement proposal does not exist")
                cur.execute(
                    """SELECT generation_status, is_stale, superseded_by
                       FROM generations WHERE id = %s FOR UPDATE""",
                    (generation_id,),
                )
                candidate = cur.fetchone()
                if (not candidate or candidate[0] != "running"
                        or candidate[1] or candidate[2] is not None):
                    raise ValueError("Replacement generation is no longer running and active")
                if generation_status not in ("success", "incomplete"):
                    raise ValueError("Replacement requires a final generation status")
                if generation_status == "success" and (
                    not script_content or not script_content.strip()
                    or not script_path or not script_path.strip()
                ):
                    raise ValueError("Successful replacement requires script content and path")

            cur.execute(
                """
                UPDATE generations
                SET generation_status = %s,
                    test_verdict      = %s,
                    script_content    = %s,
                    script_path       = %s,
                    steps_taken       = %s,
                    trace             = %s,
                    notes             = %s,
                    completed_at      = now()
                WHERE id = %s
                """,
                (
                    generation_status,
                    test_verdict,
                    script_content,
                    script_path,
                    steps_taken,
                    json.dumps(trace) if trace else None,
                    notes,
                    generation_id,
                ),
            )
            if replace_existing:
                if generation_status == "success":
                    cur.execute(
                        """UPDATE generations SET is_stale = TRUE
                           WHERE test_case_proposal_id = %s AND id <> %s
                             AND generation_status = 'success'
                             AND is_stale = FALSE AND superseded_by IS NULL
                             AND BTRIM(script_content) <> ''
                             AND BTRIM(script_path) <> ''""",
                        (proposal_id, generation_id),
                    )
                # Include the newly completed row and any surviving valid script.
                # get_conn commits all writes together or rolls them all back.
                cur.execute(
                    """UPDATE test_case_proposals
                       SET status = CASE WHEN EXISTS (
                           SELECT 1 FROM generations
                           WHERE test_case_proposal_id = %s
                             AND generation_status = 'success'
                             AND is_stale = FALSE AND superseded_by IS NULL
                             AND BTRIM(script_content) <> ''
                             AND BTRIM(script_path) <> ''
                       ) THEN 'generated' ELSE 'queued' END
                       WHERE id = %s""",
                    (proposal_id, proposal_id),
                )
            logger.info(
                "Completed generation id=%s status=%s verdict=%s steps=%s",
                generation_id,
                generation_status,
                test_verdict,
                steps_taken,
            )


def get_latest_for_proposal(proposal_id: int) -> Optional[dict]:
    """
    Returns the most recent non-superseded generation for a proposal.

    Used by the orchestrator to check current state before deciding
    whether to regenerate or just re-execute an existing script.

    Args:
        proposal_id: FK to test_case_proposals.

    Returns:
        The latest generation as a dict, or None if no generation
        exists yet for this proposal.

    Raises:
        psycopg2.DatabaseError: On any database read failure.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM   generations
                WHERE  test_case_proposal_id = %s
                  AND  superseded_by IS NULL
                ORDER  BY attempt_number DESC
                LIMIT  1
                """,
                (proposal_id,),
            )
            row = cur.fetchone()
            if not row:
                logger.debug("No generation found for proposal id=%s", proposal_id)
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
def get(generation_id: int):
    """Return one generation by id, or None."""
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM generations WHERE id = %s", (generation_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def list_for_proposal(proposal_id: int):
    """Return ALL non-superseded generations (scripts) under a scenario."""
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM generations
                   WHERE test_case_proposal_id = %s AND superseded_by IS NULL
                   ORDER BY attempt_number ASC""",
                (proposal_id,),
            )
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]
