"""
core/db/queries/executions.py

All database reads and writes for the `executions` table.

Every run is additive — results are never overwritten. Re-running a test
adds a new row; it does not update the previous one. This preserves the
complete execution history every proposal carries.
"""

import json
import logging
from typing import Optional
from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def create(
    generation_id: int,
    result: str,
    output: Optional[str],
    duration_ms: Optional[int],
    screenshot_path: Optional[str],
    triggered_by: Optional[str],
    batch_id: Optional[int] = None,
    environment: Optional[str] = None,
    step_results: Optional[list] = None,
) -> dict:
    """
    Inserts a new execution result row.

    Args:
        generation_id: FK to generations.
        result: One of 'pass', 'fail', 'skip', 'error'.
        output: Full pytest output text.
        duration_ms: How long the execution took in milliseconds.
        screenshot_path: Path to a failure screenshot, or None.
        triggered_by: Who or what ran this execution.
        batch_id: FK to batches if this execution was part of a batch.
        environment: Which environment this ran against. "It passed" is
            not auditable without saying where.
        step_results: Per-step ledger from the run — what each numbered step
            did, when, how long it took and which screenshot evidences it.
            None for scripts generated before step recording existed, which
            correctly reads as "no step detail" rather than "no steps".

    Returns:
        The newly created execution row as a dict.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO executions
                    (generation_id, result, output, duration_ms,
                     screenshot_path, triggered_by, batch_id, environment,
                     step_results)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (generation_id, result, output, duration_ms,
                 screenshot_path, triggered_by, batch_id, environment,
                 json.dumps(step_results) if step_results else None),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            result_row = dict(zip(cols, row))
            logger.info(
                "Recorded execution id=%s generation=%s result=%s",
                result_row["id"], generation_id, result,
            )
            return result_row


def list_for_generation(generation_id: int) -> list[dict]:
    """
    Returns all execution history for one generation, newest first.

    Args:
        generation_id: FK to generations.

    Returns:
        List of execution dicts, ordered by executed_at descending.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM executions
                WHERE generation_id = %s
                ORDER BY executed_at DESC
                """,
                (generation_id,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
