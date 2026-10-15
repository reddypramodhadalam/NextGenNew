"""
service/action_recorder.py

ACTION RECORDER

Records who (or what) triggered every generation, execution, and batch.
This is identity-recording only for MVP — it does not yet restrict who
can do what (user accounts and permissions are Phase 2).

This is the foundation of the audit trail. Once real user accounts exist,
this module becomes the place where authorization checks are added —
nothing else changes.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_triggerer(request_headers: dict) -> Optional[str]:
    """
    Extracts the identity of whoever triggered an action from request headers.

    For MVP, this reads a simple header (X-Triggered-By) that the frontend
    sets. In Phase 2, this will read from a real auth token.

    Args:
        request_headers: The HTTP request headers dict.

    Returns:
        The identity string, or 'anonymous' if not provided.
    """
    return request_headers.get("X-Triggered-By", "anonymous")
