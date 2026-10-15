"""
api/routes/support.py

Raising and tracking support tickets from inside NextGen2.

  GET  /v1/support/config              which provider is active, and is it usable
  GET  /v1/support/categories          the plain-English categories the form offers
  GET  /v1/support/tickets             tickets, newest first
  POST /v1/support/tickets             raise one
  GET  /v1/support/tickets/{id}        one ticket

Nothing in this file names Jira. It calls service.ticketing, which resolves
the configured provider — so moving to ServiceNow changes no route, no
response shape and no screen.

The ordering rule from help.py applies here too: literal paths are declared
before '/support/tickets/{ticket_id}', or 'config' and 'categories' would be
matched as ticket ids.

The write order is the part worth understanding. The row is inserted BEFORE
the external system is called, and updated after. If Jira is unreachable, or the
the project key is wrong, or the token expired, the ticket still exists in
NextGen2 with status='failed' and the reason recorded. A user who has just
described a problem should never be told to type it again because the
integration was down.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core.db.connection import get_conn
from core.settings import settings
from service.ticketing import TicketingError, TicketRequest, create_ticket, get_provider

logger = logging.getLogger(__name__)
router = APIRouter()


# The categories the form offers. A bounded, enumerable list rather than free
# text: each provider maps these onto its own object model — a Jira label
# today, a ServiceNow incident-versus-request decision later — and that
# mapping is only possible if the set is known in advance. The user is never
# asked to understand either model.
CATEGORIES = [
    {"key": "how-to",   "label": "How do I…?",
     "hint": "Using a feature, or finding where something lives."},
    {"key": "bug",      "label": "Something is broken",
     "hint": "A screen, run or report is not behaving as expected."},
    {"key": "data",     "label": "The data looks wrong",
     "hint": "Results, coverage or a report do not match what you expected."},
    {"key": "access",   "label": "Access or permissions",
     "hint": "You cannot reach an application, environment or integration."},
    {"key": "request",  "label": "Feature or change request",
     "hint": "Something NextGen2 does not do yet."},
]

_CATEGORY_KEYS = {c["key"] for c in CATEGORIES}


class TicketIn(BaseModel):
    """A support request submitted from the Help screen.

    Attributes:
        category: One of CATEGORIES. An unknown value is accepted rather than
            rejected — see create_ticket — because losing a written-out
            problem report over a vocabulary mismatch is the worse outcome.
        summary: One-line title.
        description: The user's account of the problem.
        context: Technical detail captured by the UI — route, application_id,
            execution_id, browser, last error.
    """

    category: str = "how-to"
    summary: str
    description: str = ""
    context: dict[str, Any] = {}


def _rows(cur) -> list[dict]:
    """Read all rows from an open cursor as dicts.

    Args:
        cur: A cursor positioned after a SELECT.

    Returns:
        Rows as dicts.
    """
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _who(request: Request) -> str:
    """Identify the caller.

    Args:
        request: The incoming request.

    Returns:
        The X-Triggered-By header value, or 'anonymous'.
    """
    return request.headers.get("X-Triggered-By", "anonymous")


# ── literal paths, before any parameterised path ─────────────────────────

@router.get("/support/config")
def support_config():
    """Report which ticketing provider is active and whether it is usable.

    A preflight check, so a missing project key surfaces here rather than at
    the moment a user is trying to report a problem.

    Returns:
        The provider name, whether its configuration validates, and the
        reason if it does not.
    """
    provider_name = settings.ticketing.provider
    try:
        provider = get_provider()
        provider.check_config()
        return {"provider": provider.name, "configured": True, "reason": None}
    except TicketingError as e:
        logger.warning("Ticketing provider is not usable: %s", e)
        return {"provider": provider_name, "configured": False, "reason": str(e)}


@router.get("/support/categories")
def list_categories():
    """Return the categories the support form offers.

    Returns:
        The bounded category list, each with a label and a hint.
    """
    return CATEGORIES


@router.get("/support/tickets")
def list_tickets(raised_by: Optional[str] = None, limit: int = 50):
    """List tickets raised from NextGen2, newest first.

    Args:
        raised_by: Restrict to one person, or None for everyone.
        limit: Maximum rows.

    Returns:
        Ticket rows including their external reference and link.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, provider, external_ref, external_url, raised_by,
                          category, summary, description, context, status,
                          external_status, submit_error, created_at
                   FROM support_tickets
                   WHERE (%s IS NULL OR raised_by = %s)
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (raised_by, raised_by, limit),
            )
            return _rows(cur)


@router.post("/support/tickets")
def raise_ticket(body: TicketIn, request: Request):
    """Raise a support ticket in the configured external system.

    Args:
        body: The support request from the form.
        request: The incoming request, for the caller's identity.

    Returns:
        The stored ticket. On failure the response still carries the ticket,
        with status 'failed' and an error explaining what went wrong.
    """
    return create_support_ticket(
        category=body.category,
        summary=body.summary,
        description=body.description,
        context=body.context,
        raised_by=_who(request),
    )


def create_support_ticket(category: str, summary: str, description: str = "",
                          context: Optional[dict] = None,
                          raised_by: str = "anonymous") -> dict:
    """Create a support ticket, whatever asked for it.

    Shared by the Help screen's form and by the assistant's raise_ticket
    action, so a ticket raised in conversation is identical to one raised on
    the form — same validation, same stored row, same failure handling. Two
    code paths would have drifted, and the one used less often would have
    been the one that was wrong.

    The local row is written FIRST and updated afterwards, so a failure to
    reach the external system loses nothing: the ticket exists in NextGen2 with
    the reason recorded, and can be retried or picked up manually. A user who
    has just described a problem should never be told to type it again
    because an integration was down.

    Args:
        category: Plain-English kind of request. An unknown value is filed as
            'how-to' rather than rejected.
        summary: One-line title. Required.
        description: The user's account of the problem.
        context: Technical detail to attach.
        raised_by: Who is asking.

    Returns:
        The stored ticket, with an `error` key added when the external system
        rejected it.
    """
    summary = (summary or "").strip()
    if not summary:
        return {"error": "A summary is required."}

    who = raised_by
    category = category if category in _CATEGORY_KEYS else "how-to"
    body = TicketIn(category=category, summary=summary,
                    description=description or "", context=context or {})
    provider_name = settings.ticketing.provider

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO support_tickets
                       (provider, raised_by, category, summary, description,
                        context, status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'open')
                   RETURNING id""",
                (provider_name, who, category, summary,
                 (body.description or "").strip(), json.dumps(body.context or {})),
            )
            ticket_id = cur.fetchone()[0]

    logger.info("Support ticket %s recorded for %s (category=%s)",
                ticket_id, who, category)

    try:
        ref = create_ticket(TicketRequest(
            category=category,
            summary=summary,
            description=(body.description or "").strip(),
            raised_by=who,
            context=body.context or {},
        ))
    except TicketingError as e:
        _mark_failed(ticket_id, str(e))
        logger.error("Support ticket %s could not be filed: %s", ticket_id, e)
        return {
            **_get(ticket_id),
            "error": (
                f"Your request was saved in NextGen2 but could not be filed "
                f"externally: {e}"
            ),
        }

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE support_tickets
                   SET provider = %s, external_ref = %s, external_url = %s,
                       external_status = %s, status = 'submitted',
                       last_synced_at = now(), updated_at = now()
                   WHERE id = %s""",
                (ref.provider, ref.external_ref, ref.external_url,
                 ref.external_status, ticket_id),
            )

    logger.info("Support ticket %s filed as %s", ticket_id, ref.external_ref)
    return _get(ticket_id)


# ── parameterised paths ──────────────────────────────────────────────────

@router.get("/support/tickets/{ticket_id}")
def get_ticket(ticket_id: int):
    """Return one ticket.

    Args:
        ticket_id: support_tickets.id.

    Returns:
        The ticket row, or an error if it does not exist.
    """
    row = _get(ticket_id)
    return row or {"error": f"Ticket {ticket_id} not found."}


# ── internals ────────────────────────────────────────────────────────────

def _get(ticket_id: int) -> dict:
    """Read one ticket.

    Args:
        ticket_id: support_tickets.id.

    Returns:
        The row as a dict, or an empty dict if there is none.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, provider, external_ref, external_url, raised_by,
                          category, summary, description, context, status,
                          external_status, submit_error, created_at
                   FROM support_tickets WHERE id = %s""",
                (ticket_id,),
            )
            rows = _rows(cur)
            return rows[0] if rows else {}


def _mark_failed(ticket_id: int, reason: str) -> None:
    """Record that a ticket could not be filed externally.

    Args:
        ticket_id: support_tickets.id.
        reason: The provider's message, truncated to a sane length.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE support_tickets
                   SET status = 'failed', submit_error = %s, updated_at = now()
                   WHERE id = %s""",
                (reason[:2000], ticket_id),
            )