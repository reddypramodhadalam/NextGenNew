"""
api/routes/help.py

The Help & Support API.

  GET    /v1/help/categories                     navigation
  GET    /v1/help/search?q=                      search articles and training docs
  GET    /v1/help/gaps                           questions that found nothing
  GET    /v1/help/records                        training record
  GET    /v1/help/training                       uploaded training documents
  POST   /v1/help/training                       upload one
  GET    /v1/help/training/{document_id}/file    download one
  POST   /v1/help/views/{view_id}/acknowledge    "I have read and understood"
  GET    /v1/help/articles                       list
  GET    /v1/help/articles/{slug}                one article, records a view
  POST   /v1/help/articles/{article_id}/approve  approve a draft

Route ordering matters here and is deliberate. FastAPI matches in declaration
order, so a literal path declared after a parameterised one on the same prefix
is swallowed by it — '/help/articles/{slug}' would happily match the literal
string 'search' as a slug. Every literal in this file is therefore declared
before any '{...}' path on the same prefix. This has bitten this codebase
before, as unexplained 405s.

Identity comes from the X-Triggered-By header, matching schedules.py,
suites.py and healing.py, rather than being passed in a body the caller
controls. It defaults to 'anonymous' and becomes a real identity when SSO
lands, with no change to the training record's shape.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.db.queries import documents as documents_q
from core.settings import settings
from service import help_service

logger = logging.getLogger(__name__)
router = APIRouter()


class ApproveIn(BaseModel):
    """Body for approving an article. Empty — the approver is the caller."""


def _who(request: Request) -> str:
    """Identify the caller.

    Args:
        request: The incoming request.

    Returns:
        The X-Triggered-By header value, or 'anonymous'.
    """
    return request.headers.get("X-Triggered-By", "anonymous")


# ── literal paths, declared before any parameterised path ────────────────

@router.get("/help/categories")
def list_categories(include_drafts: bool = False):
    """Return the article categories with their counts, for the navigation.

    Args:
        include_drafts: Whether unapproved articles are counted.

    Returns:
        A list of categories ordered so the navigation reads in a teaching
        order rather than alphabetically.
    """
    return help_service.list_categories(include_drafts=include_drafts)


@router.get("/help/search")
def search(request: Request, q: str, limit: Optional[int] = None,
           include_drafts: bool = False):
    """Search help articles and training documents together.

    A query returning nothing is recorded as a documentation gap rather than
    discarded, so the backlog of articles still to write is measured.

    Args:
        request: The incoming request, for the caller's identity.
        q: The question, in the user's own words.
        limit: Maximum passages. Defaults to the configured value.
        include_drafts: When True, unapproved articles are searchable. The
            authoring view passes True; NextGen2 AI never does.

    Returns:
        The matching passages and the query, or an empty list with
        recorded_as_gap set so the UI can offer to raise a ticket.
    """
    if not (q or "").strip():
        return {"query": q, "results": [], "recorded_as_gap": False}

    results = help_service.search(
        q, asked_by=_who(request), source="help_search",
        approved_only=not include_drafts, limit=limit,
    )
    return {
        "query": q,
        "results": results,
        "recorded_as_gap": not results,
    }


@router.get("/help/gaps")
def list_gaps(limit: int = 100):
    """Return the questions that found nothing, most frequent first.

    Args:
        limit: Maximum rows.

    Returns:
        Distinct queries with how often they were asked — the ranked list of
        articles still to be written.
    """
    return help_service.search_misses(limit=limit)


@router.get("/help/records")
def list_records(viewed_by: Optional[str] = None, acknowledged_only: bool = False):
    """Return the training record.

    Args:
        viewed_by: Restrict to one person, or None for everyone.
        acknowledged_only: When True, only acknowledged views — the subset
            that constitutes training evidence.

    Returns:
        One row per view, newest first.
    """
    return help_service.training_records(
        viewed_by=viewed_by, acknowledged_only=acknowledged_only,
    )


@router.get("/help/training")
def list_training():
    """Return the uploaded training documents.

    Returns:
        One row per document with its indexed passage count. A count of zero
        means the file carried no extractable text — usually a scanned PDF —
        and the UI surfaces it rather than letting the upload look successful.
    """
    return help_service.list_training_documents()


@router.post("/help/training")
async def upload_training(
    request: Request,
    file: UploadFile = File(..., description="Training PDF, DOCX or text file"),
    title: Optional[str] = Form(None),
):
    """Upload a training document, extract its text, and index it.

    Reuses the existing extraction path rather than adding a second one, so a
    training PDF is read exactly the way a BRD is: PyMuPDF for PDF,
    python-docx for DOCX, including text held inside tables.

    Args:
        request: The incoming request, for the caller's identity.
        file: The uploaded file.
        title: Display name. Defaults to the original filename.

    Returns:
        The created document and how many passages were indexed, with a
        warning when that count is zero.
    """
    # Imported here rather than at module load: orchestrator pulls in the
    # generation stack, and the help routes should not carry that weight just
    # to be importable.
    from service.orchestrator import _extract_text_from_document

    target_dir = Path(settings.help.training_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Keep the original name but strip any path components a client might
    # have sent, so an upload cannot write outside the training directory.
    safe_name = os.path.basename(file.filename or "training-document")
    stored_path = target_dir / safe_name

    # Never silently overwrite an existing document: a training record points
    # at a file, and replacing its content underneath would make the record
    # describe something that no longer exists.
    if stored_path.exists():
        stem, suffix = stored_path.stem, stored_path.suffix
        n = 2
        while stored_path.exists():
            stored_path = target_dir / f"{stem} ({n}){suffix}"
            n += 1

    with open(stored_path, "wb") as out:
        shutil.copyfileobj(file.file, out)

    extracted = _extract_text_from_document(str(stored_path))

    doc = documents_q.create(
        document_type="training",
        original_filename=title or safe_name,
        storage_path=str(stored_path),
        extracted_text=extracted,
    )

    indexed = help_service.index_training_document(doc["id"])
    logger.info("Training document %s uploaded by %s, %d passage(s) indexed",
                doc["id"], _who(request), indexed)

    return {
        "document": {
            "id": doc["id"],
            "original_filename": doc["original_filename"],
            "uploaded_at": doc["uploaded_at"],
        },
        "chunks_indexed": indexed,
        "warning": (
            "No text could be extracted from this file, so it will not be "
            "searchable. If it is a scanned PDF it has no text layer."
        ) if indexed == 0 else None,
    }


# ── parameterised paths ──────────────────────────────────────────────────

@router.get("/help/training/{document_id}/file")
def download_training(document_id: int):
    """Download a training document and record that it was opened.

    Args:
        document_id: documents.id of a training document.

    Returns:
        The file, or an error if it is missing from disk.
    """
    docs = [d for d in help_service.list_training_documents() if d["id"] == document_id]
    if not docs:
        return {"error": f"Training document {document_id} not found."}

    path = Path(docs[0]["storage_path"])
    if not path.exists():
        logger.error("Training document %s is recorded but missing at %s",
                     document_id, path)
        return {"error": "The file is recorded but missing from storage."}

    return FileResponse(path, filename=docs[0]["original_filename"])


@router.post("/help/views/{view_id}/acknowledge")
def acknowledge_view(view_id: int, request: Request):
    """Record an explicit "I have read and understood" against a view.

    A view says a page was opened; an acknowledgement is the assertion that
    carries weight in an audit. It is refused unless the caller is the person
    who recorded the view — an acknowledgement made on someone else's behalf
    would be worthless as evidence.

    Args:
        view_id: help_views.id.
        request: The incoming request, for the caller's identity.

    Returns:
        The updated view, or an error if it does not belong to the caller.
    """
    row = help_service.acknowledge(view_id, viewed_by=_who(request))
    if not row:
        return {"error": "No matching view for this user to acknowledge."}
    return row


@router.get("/help/articles")
def list_articles(category: Optional[str] = None, include_drafts: bool = False):
    """List the live articles.

    Args:
        category: Restrict to one category, or None for all.
        include_drafts: Whether unapproved articles are included. The Help
            screen passes True and marks them visibly as drafts.

    Returns:
        Article rows without their bodies.
    """
    return help_service.list_articles(category=category, include_drafts=include_drafts)


@router.get("/help/articles/{slug}")
def get_article(slug: str, request: Request, record_view: bool = True):
    """Return one article and record that it was read.

    Args:
        slug: The article's stable identifier.
        request: The incoming request, for the caller's identity.
        record_view: Whether to write a training record. The authoring preview
            passes False so editing an article does not manufacture evidence
            that someone was trained on it.

    Returns:
        The article, with view_id set when a view was recorded — the UI needs
        that id to offer an acknowledgement.
    """
    article = help_service.get_article(slug, include_drafts=True)
    if not article:
        return {"error": f"No help article found for '{slug}'."}

    view_id = None
    if record_view:
        view = help_service.record_view(article_id=article["id"], viewed_by=_who(request))
        view_id = view.get("id")

    return {**article, "view_id": view_id}


@router.post("/help/articles/{article_id}/approve")
def approve_article(article_id: int, request: Request):
    """Approve a draft article, making it citable and countable as training.

    Args:
        article_id: help_articles.id.
        request: The incoming request, for the caller's identity.

    Returns:
        The updated article, or an error if it does not exist or is stale.
    """
    row = help_service.approve_article(article_id, approved_by=_who(request))
    if not row:
        return {"error": f"Article {article_id} not found, or is superseded."}
    return row
