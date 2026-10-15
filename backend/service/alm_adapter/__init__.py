"""
service/alm_adapter

ALM requirements adapter modules.

This package is intentionally focused on read-only ALM ingestion.
Routes and survey wiring call these modules; no business logic should live in
api routes.
"""

from service.alm_adapter.models import ALMContext, ALMRequirement
from service.alm_adapter.requirements import (
    fetch_requirement_by_id,
    fetch_requirements,
    summarize_requirement_fields,
)

__all__ = [
    "ALMContext",
    "ALMRequirement",
    "fetch_requirements",
    "fetch_requirement_by_id",
    "summarize_requirement_fields",
]
