"""
service/jira_requirements_reader.py

Reads Jira requirements text for survey input, with optional Acceptance
Criteria enrichment.

Design goals:
  - Keep existing behavior intact when Acceptance Criteria is unavailable.
  - Avoid hard failures when projects do not use an AC custom field.
  - Keep the logic modular and traceable with explicit logging.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from service.jira_adapter import adf_to_text, load_config, _auth_header, _request
from service.jira_attachment_reader import build_image_evidence_text

logger = logging.getLogger(__name__)


BASE_FIELDS = ["summary", "description", "issuetype", "status", "updated"]


@dataclass
class RequirementBlock:
    key: str
    summary: str
    issue_type: str = ""
    status: str = ""
    description_text: str = ""
    acceptance_criteria_text: str = ""
    image_evidence_text: str = ""

    def as_block(self) -> str:
        lines = [f"[{self.key}] {self.summary}"]
        if self.issue_type:
            lines.append(f"Type: {self.issue_type}")
        if self.status:
            lines.append(f"Status: {self.status}")
        if self.description_text:
            lines.append("")
            lines.append(self.description_text.strip())
        if self.acceptance_criteria_text:
            lines.append("")
            lines.append("Acceptance Criteria:")
            lines.append(self.acceptance_criteria_text.strip())
        if self.image_evidence_text:
            lines.append("")
            lines.append(self.image_evidence_text.strip())
        return "\n".join(lines)


def _search_path(api_version: str) -> str:
    return "/rest/api/3/search/jql" if api_version == "3" else "/rest/api/2/search"


def _coerce_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return adf_to_text(value).strip()


def _read_issue_with_names(base_url: str, api_version: str, auth_header: str, issue_key: str) -> dict:
    encoded_key = urllib.parse.quote(issue_key, safe="")
    url = f"{base_url}/rest/api/{api_version}/issue/{encoded_key}?expand=names"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": auth_header,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        import json

        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _detect_acceptance_field_id(base_url: str, api_version: str, auth_header: str, issue_key: str) -> Optional[str]:
    try:
        payload = _read_issue_with_names(base_url, api_version, auth_header, issue_key)
    except Exception as exc:
        logger.warning("Could not auto-detect Acceptance Criteria field from issue %s: %s", issue_key, exc)
        return None

    names = payload.get("names", {}) or {}
    fields = payload.get("fields", {}) or {}

    # Prefer labels that contain both terms to avoid false positives.
    matches = []
    for field_id, display_name in names.items():
        label = (display_name or "").strip().lower()
        if "accept" in label and "criteria" in label:
            value = fields.get(field_id)
            matches.append((field_id, value not in (None, "", [], {})))

    if not matches:
        logger.info("No Acceptance Criteria field label detected for issue %s", issue_key)
        return None

    # Prefer non-empty on this issue, otherwise still return the first match.
    for field_id, non_empty in matches:
        if non_empty:
            logger.info("Detected Acceptance Criteria field %s from issue %s", field_id, issue_key)
            return field_id
    logger.info("Detected Acceptance Criteria field %s from issue %s (empty on this issue)", matches[0][0], issue_key)
    return matches[0][0]


def _search_blocks(
    cfg,
    jql: str,
    page_size: int,
    acceptance_field_id: Optional[str],
    include_images: bool = False,
    auth_header: Optional[str] = None,
) -> list[RequirementBlock]:
    out: list[RequirementBlock] = []
    start_at = 0
    fields = list(BASE_FIELDS)
    if acceptance_field_id:
        fields.append(acceptance_field_id)
    if include_images:
        fields.append("attachment")

    while True:
        payload = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": page_size,
            "fields": fields,
        }
        data = _request(cfg, _search_path(cfg.api_version), payload)
        issues = data.get("issues", []) or []
        total = int(data.get("total", 0))

        for issue in issues:
            issue_fields = issue.get("fields", {}) or {}
            issue_key = issue.get("key", "")

            image_evidence_text = ""
            if include_images and auth_header:
                try:
                    image_evidence_text = build_image_evidence_text(
                        issue_key=issue_key,
                        raw_attachments=issue_fields.get("attachment"),
                        auth_header=auth_header,
                    )
                except Exception as exc:
                    # Image evidence is strictly additive — never let it break
                    # the requirement read itself.
                    logger.warning(
                        "Image evidence extraction failed for issue %s: %s",
                        issue_key, exc,
                    )

            out.append(
                RequirementBlock(
                    key=issue_key,
                    summary=issue_fields.get("summary") or "",
                    issue_type=(issue_fields.get("issuetype") or {}).get("name", ""),
                    status=(issue_fields.get("status") or {}).get("name", ""),
                    description_text=_coerce_text(issue_fields.get("description")),
                    acceptance_criteria_text=_coerce_text(issue_fields.get(acceptance_field_id)) if acceptance_field_id else "",
                    image_evidence_text=image_evidence_text,
                )
            )

        start_at += len(issues)
        if start_at >= total or not issues:
            break

    return out


def load_requirements_text(
    project_key: Optional[str] = None,
    issue_type: Optional[str] = None,
    issue_key: Optional[str] = None,
    page_size: int = 50,
) -> str:
    """
    Return BRD-equivalent requirement text from Jira, optionally enriched with
    an Acceptance Criteria custom field.

    AC field resolution order:
      1) JIRA_ACCEPTANCE_CRITERIA_FIELD_ID from environment, if provided.
      2) Auto-detect by issue label lookup (expand=names) from a probe issue.
      3) Fallback to existing behavior with no AC enrichment.
    """
    cfg = load_config(project_key=project_key, issue_type=issue_type)
    jql = f'key = "{issue_key}"' if issue_key else cfg.jql

    acceptance_field_id = (os.environ.get("JIRA_ACCEPTANCE_CRITERIA_FIELD_ID") or "").strip() or None
    auth_header = _auth_header(cfg)

    if not acceptance_field_id:
        probe_key = issue_key
        if not probe_key:
            preview = _request(
                cfg,
                _search_path(cfg.api_version),
                {
                    "jql": cfg.jql,
                    "startAt": 0,
                    "maxResults": 1,
                    "fields": ["key"],
                },
            )
            probe_issues = preview.get("issues", []) or []
            probe_key = probe_issues[0].get("key") if probe_issues else None

        if probe_key:
            acceptance_field_id = _detect_acceptance_field_id(
                base_url=cfg.base_url,
                api_version=cfg.api_version,
                auth_header=auth_header,
                issue_key=probe_key,
            )

    # Image attachment evidence is only attempted for a single, explicitly
    # scoped issue. It performs real network downloads per image, so it is
    # deliberately not run for bulk/project-wide reads.
    include_images = bool(issue_key)

    blocks = _search_blocks(
        cfg=cfg,
        jql=jql,
        page_size=page_size,
        acceptance_field_id=acceptance_field_id,
        include_images=include_images,
        auth_header=auth_header if include_images else None,
    )

    with_ac = sum(1 for b in blocks if b.acceptance_criteria_text)
    with_images = sum(1 for b in blocks if b.image_evidence_text)
    logger.info(
        "Jira requirements loaded: count=%d issue_key=%s ac_field=%s with_ac=%d with_images=%d",
        len(blocks),
        issue_key or "<all>",
        acceptance_field_id or "<none>",
        with_ac,
        with_images,
    )

    return "\n\n".join(b.as_block() for b in blocks if b.as_block().strip())
