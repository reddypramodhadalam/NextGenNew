"""
service/alm_adapter/mapping.py

Field extraction and normalization for ALM entities.
"""

from service.alm_adapter.models import ALMRequirement


def extract_fields(entity: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in entity.get("Fields", []) or []:
        name = str(f.get("Name", "")).strip()
        if not name:
            continue
        values = f.get("values", []) or []
        value = values[0].get("value", "") if values else ""
        out[name] = str(value or "")
    return out


def map_requirement(entity: dict) -> ALMRequirement:
    fields = extract_fields(entity)
    return ALMRequirement(
        requirement_id=str(fields.get("id", "")).strip(),
        title=str(fields.get("name", "")).strip(),
        description=str(fields.get("description", "")).strip(),
        status=str(fields.get("status", "")).strip(),
        priority=str(fields.get("req-priority", "")).strip(),
        product=str(fields.get("req-product", "")).strip(),
        owner=str(fields.get("owner", "")).strip(),
        target_release=str(fields.get("target-rel", "")).strip(),
        updated_at=str(fields.get("last-modified", "")).strip(),
        created_at=str(fields.get("creation-time", "")).strip(),
        raw_fields=fields,
        raw=entity,
    )
