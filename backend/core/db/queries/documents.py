"""
core/db/queries/documents.py

All database reads and writes for the `documents` table.
Documents include BRD/FRD requirements, imported test case files,
and knowledge base standards.
"""

import logging
from typing import Optional
from core.db.connection import get_conn

logger = logging.getLogger(__name__)


def create(
    document_type: str,
    original_filename: str,
    storage_path: str,
    extracted_text: Optional[str] = None,
    application_id: Optional[int] = None,
) -> dict:
    """
    Inserts a new document record.

    Args:
        document_type: One of 'requirements', 'existing_test_cases', 'standard'.
        original_filename: The filename as uploaded.
        storage_path: Where the file is stored on disk.
        extracted_text: Parsed text content, used for generation and search.
        application_id: FK to applications, or None for org-wide standards.

    Returns:
        The newly created document row as a dict.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (document_type, original_filename, storage_path,
                     extracted_text, application_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (document_type, original_filename, storage_path,
                 extracted_text, application_id),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
