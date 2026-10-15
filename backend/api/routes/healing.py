"""
api/routes/healing.py

Self-healing endpoints.

  POST /v1/generations/{id}/diagnose   — work out why a test failed and
                                         PROPOSE a selector fix
  GET  /v1/generations/{id}/healing    — healing history for a script
  POST /v1/healing/{id}/approve        — apply the fix (human decision)
  POST /v1/healing/{id}/reject         — record a rejection

Diagnosis never changes a script. Applying a fix requires an explicit
approval, and the decision is recorded with who made it and when — a test
that silently rewrites itself could drift away from what it was validated
to check, and could hide a real defect behind a "healed" selector.
"""

import logging
from fastapi import APIRouter, Request
from pydantic import BaseModel

from service import healing as healing_service

logger = logging.getLogger(__name__)
router = APIRouter()


class DiagnoseRequest(BaseModel):
    execution_id: int | None = None


@router.post("/generations/{generation_id}/diagnose")
def diagnose(generation_id: int, body: DiagnoseRequest | None = None):
    """
    Diagnose a failed run and propose a fix.

    Opens the application as it is now, compares it with what the script
    expects, and records a proposal for review. Takes 30-60 seconds — it
    loads the page and makes one model call.
    """
    execution_id = body.execution_id if body else None
    logger.info("Diagnosing generation %s (execution=%s)", generation_id, execution_id)
    return healing_service.diagnose_failure(generation_id, execution_id)


@router.get("/generations/{generation_id}/healing")
def list_healing(generation_id: int):
    """Return every healing proposal raised for this script."""
    return {"proposals": healing_service.list_for_generation(generation_id)}


@router.post("/healing/{healing_id}/approve")
def approve(healing_id: int, request: Request):
    """Apply the proposed selector change. Records who approved it."""
    who = request.headers.get("X-Triggered-By", "anonymous")
    return healing_service.apply_healing(healing_id, decided_by=who)


@router.post("/healing/{healing_id}/reject")
def reject(healing_id: int, request: Request):
    """Reject the proposal, leaving the script untouched."""
    who = request.headers.get("X-Triggered-By", "anonymous")
    return healing_service.reject_healing(healing_id, decided_by=who)
