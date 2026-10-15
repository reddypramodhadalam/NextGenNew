"""
core/db/queries/healing.py

All database reads and writes for the `healing_proposals` table.
Healing proposals are never applied automatically — they always
require explicit human approval.
"""

import logging
from typing import Optional
from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def create(execution_id: int, old_selector: str, proposed_role: str,
           proposed_name: str, confidence: str, reasoning: str) -> dict:
    """
    Creates a new healing proposal from the Healer Agent's output.

    Args:
        execution_id: FK to executions (the failed run that triggered healing).
        old_selector: The CSS selector that stopped working.
        proposed_role: The role of the replacement element.
        proposed_name: The name/label of the replacement element.
        confidence: 'high', 'medium', or 'low'.
        reasoning: The agent's explanation for its proposal.

    Returns:
        The newly created healing_proposals row as a dict.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO healing_proposals
                    (execution_id, old_selector, proposed_role,
                     proposed_name, confidence, reasoning)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (execution_id, old_selector, proposed_role,
                 proposed_name, confidence, reasoning),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))


def approve(proposal_id: int, decided_by: Optional[str]) -> None:
    """
    Marks a proposal as approved. The actual script patching is
    handled by the service layer after calling this.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE healing_proposals
                SET status = 'approved', decided_at = now(), decided_by = %s
                WHERE id = %s
                """,
                (decided_by, proposal_id),
            )


def reject(proposal_id: int, decided_by: Optional[str]) -> None:
    """Marks a proposal as rejected."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE healing_proposals
                SET status = 'rejected', decided_at = now(), decided_by = %s
                WHERE id = %s
                """,
                (decided_by, proposal_id),
            )
