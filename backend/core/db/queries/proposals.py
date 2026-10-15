"""
core/db/queries/proposals.py

All database reads and writes for the `test_case_proposals` table.
"""

import json
import logging
from typing import Optional
from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def create(
    application_id: int,
    title: str,
    description: str,
    source: str,
    test_type: str = "functional",
    category: Optional[str] = None,
    import_batch_id: Optional[int] = None,
    source_document_id: Optional[int] = None,
    steps: Optional[list] = None,
    derivation: Optional[str] = None,
    origin: Optional[str] = None,
    source_version: Optional[str] = None,
    source_updated_at: Optional[str] = None,
) -> dict:
    """
    Inserts a new test case proposal.

    Args:
        application_id: FK to applications.
        title: One-line scenario name.
        description: Full steps/content the Explorer Agent will carry out.
        source: One of 'author_agent', 'bulk_import', 'brd_frd'.
        test_type: Defaults to 'functional'.
        category: One of 'positive', 'negative', 'edge', 'boundary',
            'wrong_data', 'missing_field', 'security', or None.
        import_batch_id: FK to import_batches, if sourced from a file.
        source_document_id: FK to documents, if grounded in a BRD/FRD.
        steps: Optional structured steps, [{"n", "step", "expected",
            "matched", "element_hint"}]. When present, `description` must
            already be the flattened projection of it — see
            service.test_case_import.flatten_steps. None means a one-line
            test case, which is what every pre-006 row is.

    Returns:
        The newly created proposal as a dict.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO test_case_proposals
                    (application_id, title, description, source, test_type,
                     category, import_batch_id, source_document_id, steps,
                     derivation, origin, source_version, source_updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (application_id, title, description, source, test_type,
                 category, import_batch_id, source_document_id,
                 json.dumps(steps) if steps else None,
                 derivation, origin, source_version, source_updated_at),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
            logger.info(
                "Created proposal id=%s title=%s app=%s",
                result["id"], title, application_id,
            )
            return result


def list_for_application(application_id: int,
                         status_filter: Optional[str] = None) -> list[dict]:
    """
    Returns all proposals for an application, newest first.

    Args:
        application_id: FK to applications.
        status_filter: Optional status to filter by (e.g. 'generated').

    Returns:
        List of proposal dicts.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            if status_filter:
                cur.execute(
                    """
                    SELECT * FROM test_case_proposals
                    WHERE application_id = %s AND status = %s
                    ORDER BY created_at DESC
                    """,
                    (application_id, status_filter),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM test_case_proposals
                    WHERE application_id = %s AND status != 'archived'
                    ORDER BY created_at DESC
                    """,
                    (application_id,),
                )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def update_status(proposal_id: int, status: str) -> None:
    """
    Updates a proposal's status.

    Args:
        proposal_id: The proposal's database ID.
        status: The new status value. Must be a valid status from the
            CHECK constraint in the schema.

    Returns: None
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE test_case_proposals
                SET status = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, proposal_id),
            )
            logger.info("Proposal id=%s → status=%s", proposal_id, status)


def update_priority(proposal_id: int, priority: str) -> None:
    """
    Sets the priority level (P0–P4) after human review.

    Args:
        proposal_id: The proposal's database ID.
        priority: One of 'P0', 'P1', 'P2', 'P3', 'P4'.

    Returns: None
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE test_case_proposals
                SET priority = %s, updated_at = now()
                WHERE id = %s
                """,
                (priority, proposal_id),
            )


def update_steps(proposal_id: int, steps: list, description: str,
                 edited_by: Optional[str] = None) -> Optional[dict]:
    """
    Replace a test case's steps and re-derive its description, in one
    transaction, recording the change in proposal_edits.

    Two invariants live here:

    1. steps, description and tags.step_count are written TOGETHER. Both
       of the latter are projections of steps, so updating one without the
       others leaves the row saying two different things — and generation
       reads description, while list screens read step_count.
    2. the previous value is recorded first. An approved test case is the
       audit artefact; changing its steps after approval is exactly the
       event proposal_edits exists to capture, and until now nothing had
       ever written to that table.

    Args:
        proposal_id: The test case being edited.
        steps: The new steps, already validated and numbered by the caller.
        description: flatten_steps(steps) — passed in rather than computed
            here so the query layer keeps no opinion about the format.
        edited_by: Who made the change.

    Returns:
        The updated proposal row, or None if the id does not exist.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT steps FROM test_case_proposals WHERE id = %s",
                (proposal_id,),
            )
            row = cur.fetchone()
            if not row:
                logger.warning("Cannot edit steps: proposal id=%s not found", proposal_id)
                return None
            # description is not recorded: it is derived from steps, so the
            # steps before and after are the whole of the change.
            previous_steps = row[0]

            cur.execute(
                """
                INSERT INTO proposal_edits
                    (test_case_proposal_id, field_edited, previous_value,
                     new_value, edited_by)
                VALUES (%s, 'steps', %s, %s, %s)
                """,
                (
                    proposal_id,
                    # NOT NULL in the schema — a test case that had no steps
                    # before still needs an honest previous value.
                    json.dumps(previous_steps) if previous_steps else "[]",
                    json.dumps(steps),
                    edited_by,
                ),
            )

            cur.execute(
                """
                UPDATE test_case_proposals
                SET steps       = %s,
                    description = %s,
                    -- tags.step_count is a cached count that list screens
                    -- read instead of parsing the JSONB. It has to move with
                    -- the steps or it silently reports the old number.
                    tags        = jsonb_set(
                                      COALESCE(tags, '{}'::jsonb),
                                      '{step_count}',
                                      to_jsonb(%s::int)
                                  ),
                    updated_at  = now()
                WHERE id = %s
                RETURNING *
                """,
                (json.dumps(steps), description, len(steps), proposal_id),
            )
            updated = cur.fetchone()
            cols = [d[0] for d in cur.description]

    logger.info(
        "Proposal id=%s steps edited by %s (%d step(s), was %d)",
        proposal_id, edited_by or "unknown", len(steps),
        len(previous_steps or []),
    )
    return dict(zip(cols, updated))


def get(proposal_id: int):
    """
    Return one proposal by id, including its category and priority.
    Used by the scripts endpoint to show category/priority badges.
    """
    from core.db.connection import get_conn
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM test_case_proposals WHERE id = %s", (proposal_id,))
            row = cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def set_ai_cost(proposal_id, ai_cost_usd, survey_run_id):
    """
    Saves a test case's estimated share of its survey run's AI cost.
    The cost is an even split of the run, not a measured per-test-case figure.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE test_case_proposals SET ai_cost_usd = %s, survey_run_id = %s WHERE id = %s",
                (ai_cost_usd, survey_run_id, proposal_id),
            )
            