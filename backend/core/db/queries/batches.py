"""
core/db/queries/batches.py

All database reads and writes for the `batches` table.
"""

import logging
from typing import Optional
from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def create(application_id: int, environment: str,
           triggered_by: Optional[str], trigger_type: str) -> dict:
    """
    Creates a new batch run record.

    Args:
        application_id: FK to applications.
        environment: Which environment this batch runs against.
        triggered_by: Who or what started it.
        trigger_type: 'manual' or 'scheduled'.

    Returns:
        The newly created batch row as a dict.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO batches
                    (application_id, environment, triggered_by, trigger_type)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (application_id, environment, triggered_by, trigger_type),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def update_status(batch_id: int, status: str) -> None:
    """
    Updates a batch's status.

    Args:
        batch_id: The batch's database ID.
        status: One of 'running', 'completed', 'failed', 'cancelled'.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE batches
                SET status = %s,
                    completed_at = CASE WHEN %s != 'running' THEN now() ELSE NULL END
                WHERE id = %s
                """,
                (status, status, batch_id),
            )
