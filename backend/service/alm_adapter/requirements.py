"""
service/alm_adapter/requirements.py

Read ALM requirements with pagination and field inventory.
"""

import logging
from collections import Counter

from service.alm_adapter.client import ALMClient
from service.alm_adapter.mapping import map_requirement
from service.alm_adapter.models import ALMContext, ALMRequirement

logger = logging.getLogger(__name__)


def _headers(ctx: ALMContext) -> dict[str, str]:
    cookie = f"LWSSO_COOKIE_KEY={ctx.lwsso_cookie_key}"
    if ctx.qc_session:
        cookie += f"; QCSession={ctx.qc_session}"
    return {
        "Cookie": cookie,
        "Accept": "application/json",
    }


def _requirements_url(ctx: ALMContext) -> str:
    return f"{ctx.base_url}/rest/domains/{ctx.domain}/projects/{ctx.project}/requirements"


def fetch_requirements(ctx: ALMContext, page: int = 1, page_size: int = 50) -> dict:
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))
    start_index = (page - 1) * page_size + 1

    client = ALMClient()
    resp = client.get(
        _requirements_url(ctx),
        headers=_headers(ctx),
        params={"page-size": page_size, "start-index": start_index},
    )
    if resp.status_code == 401:
        raise PermissionError("ALM authentication expired or invalid")
    resp.raise_for_status()

    body = resp.json()
    entities = body.get("entities", []) or []
    items = [map_requirement(e) for e in entities]
    total = int(body.get("TotalResults", len(items)))

    logger.info(
        "alm_requirements: fetched page=%d page_size=%d count=%d total=%d domain=%s project=%s",
        page, page_size, len(items), total, ctx.domain, ctx.project,
    )

    return {
        "data": [
            {
                "id": r.requirement_id,
                "title": r.title,
                "description": r.description,
                "status": r.status,
                "priority": r.priority,
                "product": r.product,
                "owner": r.owner,
                "target_release": r.target_release,
                "updated_at": r.updated_at,
                "created_at": r.created_at,
                "raw_fields": r.raw_fields,
            }
            for r in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "source": "alm",
    }


def fetch_requirement_by_id(ctx: ALMContext, requirement_id: str) -> dict:
    rid = str(requirement_id or "").strip()
    if not rid:
        raise ValueError("requirement_id is required")

    client = ALMClient()
    url = f"{_requirements_url(ctx)}/{rid}"
    resp = client.get(url, headers=_headers(ctx))
    if resp.status_code == 404:
        raise LookupError(f"Requirement '{rid}' not found")
    if resp.status_code == 401:
        raise PermissionError("ALM authentication expired or invalid")
    resp.raise_for_status()

    body = resp.json()
    entity = body.get("entity", body)
    mapped = map_requirement(entity)
    return {
        "id": mapped.requirement_id,
        "title": mapped.title,
        "description": mapped.description,
        "status": mapped.status,
        "priority": mapped.priority,
        "product": mapped.product,
        "owner": mapped.owner,
        "target_release": mapped.target_release,
        "updated_at": mapped.updated_at,
        "created_at": mapped.created_at,
        "raw_fields": mapped.raw_fields,
    }


def summarize_requirement_fields(
    ctx: ALMContext,
    pages: int = 3,
    page_size: int = 50,
) -> dict:
    pages = max(1, min(int(pages), 20))
    page_size = max(1, min(int(page_size), 200))

    present = Counter()
    non_empty = Counter()
    rows_seen = 0

    for page in range(1, pages + 1):
        payload = fetch_requirements(ctx, page=page, page_size=page_size)
        rows = payload.get("data", [])
        rows_seen += len(rows)
        for row in rows:
            fields = row.get("raw_fields", {}) or {}
            for name, value in fields.items():
                present[name] += 1
                if str(value or "").strip():
                    non_empty[name] += 1

    summary = [
        {
            "field": name,
            "present_count": present[name],
            "non_empty_count": non_empty.get(name, 0),
        }
        for name in sorted(present.keys())
    ]

    logger.info(
        "alm_requirements: field summary rows_seen=%d fields=%d domain=%s project=%s",
        rows_seen, len(summary), ctx.domain, ctx.project,
    )

    return {
        "rows_seen": rows_seen,
        "pages_sampled": pages,
        "page_size": page_size,
        "fields": summary,
    }
