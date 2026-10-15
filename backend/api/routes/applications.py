"""
api/routes/applications.py

GET  /v1/applications                         — list all applications
POST /v1/applications                         — create or find by URL (idempotent)
GET  /v1/applications/{id}                    — get one application
PATCH /v1/applications/{id}/sso-gateway       — toggle SSO flag
POST /v1/applications/{id}/crawl              — start App Map crawl (SSE stream)
GET  /v1/applications/{id}/maps/latest        — latest completed App Map summary
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.sse import event_stream, make_progress_queue, queue_to_stream
from api.routes.workflows import WorkflowRoute
from core.db.queries import applications as apps_db
from service.context_resolver import normalize_url

logger = logging.getLogger(__name__)
router = APIRouter(route_class=WorkflowRoute)


class CreateApplicationRequest(BaseModel):
    name: str
    base_url: Optional[str] = None
    owner: Optional[str] = None
    requires_sso_gateway: bool = False


class UpdateSsoGatewayRequest(BaseModel):
    requires_sso_gateway: bool


@router.get("/applications")
def list_applications():
    """Returns all active (non-archived) applications."""
    logger.debug("list_applications: request received")
    result = apps_db.list_all(include_archived=False)
    logger.info("list_applications: returning %d application(s)", len(result) if isinstance(result, list) else 1)
    return result


@router.post("/applications")
def create_or_find_application(body: CreateApplicationRequest):
    """
    Creates a new application, or returns an existing one if the URL matches.
    Idempotent by normalized URL.
    """
    logger.info(
        "create_or_find_application: name=%r  base_url=%r  sso=%s",
        body.name, body.base_url, body.requires_sso_gateway,
    )
    normalized = normalize_url(body.base_url) if body.base_url else None

    if normalized:
        existing = apps_db.find_by_normalized_url(normalized, include_archived=True)
        if existing:
            if existing.get("is_archived"):
                logger.info(
                    "create_or_find_application: restoring archived application id=%d",
                    existing["id"],
                )
                restored = apps_db.unarchive(
                    application_id=existing["id"],
                    name=body.name,
                    base_url=body.base_url,
                    owner=body.owner,
                )
                restored = apps_db.set_requires_sso_gateway(
                    restored["id"],
                    body.requires_sso_gateway,
                ) or restored
                return {**restored, "created": False, "restored": True}
            logger.info(
                "create_or_find_application: found existing application id=%d",
                existing["id"],
            )
            updated = apps_db.set_requires_sso_gateway(
                existing["id"],
                body.requires_sso_gateway,
            ) or existing
            return {**updated, "created": False}

    application = apps_db.create(
        name=body.name,
        base_url=body.base_url,
        normalized_url=normalized,
        application_type=None,
        owner=body.owner,
        requires_sso_gateway=body.requires_sso_gateway,
    )
    logger.info(
        "create_or_find_application: created new application id=%d  name=%r",
        application.get("id"), body.name,
    )
    return {**application, "created": True}


@router.get("/applications/{application_id}")
def get_application(application_id: int):
    """Returns one application by ID."""
    logger.debug("get_application: id=%d", application_id)
    application = apps_db.find_by_id(application_id)
    if not application:
        logger.warning("get_application: not found  id=%d", application_id)
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": f"Application {application_id} not found."})
    logger.debug("get_application: found  id=%d  name=%r", application_id, application.get("name"))
    return application


@router.patch("/applications/{application_id}/sso-gateway")
def set_application_sso_gateway(application_id: int, body: UpdateSsoGatewayRequest):
    """Enable or disable SSO gateway enforcement for an existing application."""
    logger.info(
        "set_application_sso_gateway: id=%d  requires_sso_gateway=%s",
        application_id, body.requires_sso_gateway,
    )
    updated = apps_db.set_requires_sso_gateway(application_id, body.requires_sso_gateway)
    if not updated:
        logger.warning("set_application_sso_gateway: not found  id=%d", application_id)
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": f"Application {application_id} not found."},
        )
    logger.debug("set_application_sso_gateway: updated  id=%d", application_id)
    return updated


# ── App Map crawler ──────────────────────────────────────────────────────────


@router.post("/applications/{application_id}/crawl")
async def start_crawl(
    application_id: int,
    request: Request,
    username: Optional[str] = Form(None, description="Username/email for authenticated crawl"),
    password: Optional[str] = Form(None, description="Password for authenticated crawl"),
):
    """
    Start an App Map crawl for this application.

    Streams live progress via SSE while the crawler visits pages and
    records interactive elements. The map is persisted in the
    application_maps table when complete.

    Credentials are optional. If provided, the crawler detects the login
    form at the landing page, fills it, and maps the authenticated session.

    Final SSE event (type='complete'):
        map_id, page_count, element_count, message

    Returns:
        SSE stream (text/event-stream)

    Raises:
        404: If the application is not found or has no base_url.
    """
    app = apps_db.find_by_id(application_id)
    if not app:
        logger.warning("start_crawl: application not found  id=%d", application_id)
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND",
                    "message": f"Application {application_id} not found."},
        )
    base_url = app.get("base_url")
    if not base_url:
        logger.warning("start_crawl: application %d has no base_url", application_id)
        raise HTTPException(
            status_code=400,
            detail={"code": "NO_URL",
                    "message": "Application has no base_url — set one before crawling."},
        )
    logger.info(
        "start_crawl: application_id=%d  base_url=%s  authenticated=%s",
        application_id, base_url, bool(username or password),
    )

    credentials = None
    if username or password:
        credentials = {
            "username": username or "",
            "password": password or "",
        }

    queue, progress_callback = make_progress_queue()
    loop = asyncio.get_event_loop()

    async def run():
        from service.app_map_crawler import crawl_application

        def sync_progress(event_type: str, data: dict) -> None:
            asyncio.run_coroutine_threadsafe(
                progress_callback(event_type, data), loop
            )

        try:
            await asyncio.to_thread(
                crawl_application,
                application_id,
                base_url,
                credentials,
                None,       # brd_text — not available at crawl-only route; survey runner passes it
                sync_progress,
            )
        except Exception as exc:
            logger.exception("Crawl failed for application %d", application_id)
            await progress_callback("error", {"message": str(exc)})

    asyncio.create_task(run())

    return StreamingResponse(
        event_stream(queue_to_stream(queue)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/applications/{application_id}/maps/latest")
def get_latest_map(application_id: int):
    """
    Return summary of the most recent completed App Map for this application.

    Used by the UI to show crawl status and by the generation pipeline to
    check whether a fresh crawl is needed.

    Returns:
        {id, crawled_at, page_count, element_count, was_authenticated}
        or 404 if no crawl has been completed for this application.
    """
    from service.app_map_crawler import get_latest_map as _get_latest_map
    result = _get_latest_map(application_id)
    if not result:
        logger.warning("get_latest_map: no completed map  application_id=%d", application_id)
        raise HTTPException(
            status_code=404,
            detail={"code": "NO_MAP",
                    "message": "No App Map found — run a crawl first."},
        )
    # Don't return map_data in the summary — it can be several MB.
    return {k: v for k, v in result.items() if k != "map_data"}
