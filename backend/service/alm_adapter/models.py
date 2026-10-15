"""
service/alm_adapter/models.py

Typed models for ALM read operations.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ALMContext:
    base_url: str
    domain: str
    project: str
    lwsso_cookie_key: str
    qc_session: str


@dataclass
class ALMRequirement:
    requirement_id: str
    title: str
    description: str
    status: str
    priority: str
    product: str
    owner: str
    target_release: str
    updated_at: str
    created_at: str
    raw_fields: dict[str, str]
    raw: dict[str, Any]
