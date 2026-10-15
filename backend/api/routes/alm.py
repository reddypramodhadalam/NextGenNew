"""
api/routes/alm.py

Read-only ALM endpoints for requirements ingestion.
Credentials are supplied per request and are never persisted.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.settings import settings
from service.alm_adapter.auth import login
from service.alm_adapter.requirements import (
    fetch_requirement_by_id,
    fetch_requirements,
    summarize_requirement_fields,
)

logger = logging.getLogger(__name__)
from api.routes.workflows import WorkflowRoute

router = APIRouter(route_class=WorkflowRoute)


class ALMAuthIn(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    project: str = Field(..., min_length=1)
    base_url: Optional[str] = None


class ALMRequirementsIn(ALMAuthIn):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class ALMFieldsIn(ALMAuthIn):
    pages: int = Field(default=3, ge=1, le=20)
    page_size: int = Field(default=50, ge=1, le=200)


def _resolve_base_url(body_base_url: Optional[str]) -> str:
    base_url = (body_base_url or settings.alm.base_url or "").strip()
    if not base_url:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ALM_BASE_URL_REQUIRED",
                "message": "ALM base URL is required. Provide base_url or configure ALM_BASE_URL.",
            },
        )
    return base_url


def _auth(body: ALMAuthIn):
    try:
        return login(
            base_url=_resolve_base_url(body.base_url),
            username=body.username,
            password=body.password,
            domain=body.domain,
            project=body.project,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail={"code": "ALM_AUTH_FAILED", "message": str(exc)})
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail={"code": "ALM_AUTH_FAILED", "message": str(exc)})
    except Exception as exc:
        logger.exception("ALM authentication error for domain=%s project=%s", body.domain, body.project)
        raise HTTPException(status_code=502, detail={"code": "ALM_UPSTREAM_ERROR", "message": str(exc)})


@router.post("/alm/requirements")
def list_alm_requirements(body: ALMRequirementsIn):
    ctx = _auth(body)
    try:
        payload = fetch_requirements(ctx, page=body.page, page_size=body.page_size)
        payload["domain"] = body.domain
        payload["project"] = body.project
        return payload
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail={"code": "ALM_AUTH_EXPIRED", "message": str(exc)})
    except Exception as exc:
        logger.exception(
            "ALM requirements fetch failed domain=%s project=%s page=%d",
            body.domain, body.project, body.page,
        )
        raise HTTPException(status_code=502, detail={"code": "ALM_REQUIREMENTS_FETCH_FAILED", "message": str(exc)})


@router.post("/alm/requirements/{requirement_id}")
def get_alm_requirement(requirement_id: str, body: ALMAuthIn):
    ctx = _auth(body)
    try:
        data = fetch_requirement_by_id(ctx, requirement_id=requirement_id)
        return {"data": data, "domain": body.domain, "project": body.project}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail={"code": "ALM_REQUIREMENT_NOT_FOUND", "message": str(exc)})
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail={"code": "ALM_AUTH_EXPIRED", "message": str(exc)})
    except Exception as exc:
        logger.exception(
            "ALM requirement fetch failed id=%s domain=%s project=%s",
            requirement_id, body.domain, body.project,
        )
        raise HTTPException(status_code=502, detail={"code": "ALM_REQUIREMENT_FETCH_FAILED", "message": str(exc)})


@router.post("/alm/requirements/fields")
def list_alm_requirement_fields(body: ALMFieldsIn):
    ctx = _auth(body)
    try:
        payload = summarize_requirement_fields(ctx, pages=body.pages, page_size=body.page_size)
        payload["domain"] = body.domain
        payload["project"] = body.project
        return payload
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail={"code": "ALM_AUTH_EXPIRED", "message": str(exc)})
    except Exception as exc:
        logger.exception(
            "ALM requirement field summary failed domain=%s project=%s",
            body.domain, body.project,
        )
        raise HTTPException(status_code=502, detail={"code": "ALM_FIELDS_FETCH_FAILED", "message": str(exc)})
