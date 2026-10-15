"""
service/generation_contract.py

Structured generation contract between accepted proposals and script generation.

This module keeps navigation/setup requirements explicit so generation can be
validated against what the scenario actually needs.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


_AUTH_RE = re.compile(r"\b(authenticated|logged in|sign in|login)\b", re.IGNORECASE)
_NAV_RE = re.compile(
    r"\b(navigate|go to|open|menu|tab|screen|page|home|dashboard|settings|profile|url|route)\b",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(
    r"\b(field|input|textbox|text box|dropdown|drop-down|form|enter|type|value|placeholder)\b",
    re.IGNORECASE,
)
_TARGET_RE = re.compile(r"\bon the ([a-z0-9 _\-/]+?) (?:page|screen)\b", re.IGNORECASE)
_NAV_OUTCOME_RE = re.compile(
    r"\b(navigate|go to|redirect|link|opens .* page|opens .* portal|opens .* site)\b",
    re.IGNORECASE,
)


def build_generation_contract(
    *,
    title: str,
    steps: Optional[list[dict]],
    preconditions: Optional[list],
) -> dict:
    """Create a structured contract used by generator prompt + validator."""
    clean_steps = [s for s in (steps or []) if isinstance(s, dict)]
    step_texts = [str(s.get("step") or "").strip() for s in clean_steps]
    step_texts = [t for t in step_texts if t]

    clean_preconditions = [str(p).strip() for p in (preconditions or []) if str(p).strip()]
    pre_text = " \n".join(clean_preconditions)
    step_text = " \n".join(step_texts)

    authentication_required = bool(_AUTH_RE.search(pre_text))
    has_navigation_precondition = bool(_NAV_RE.search(pre_text))
    has_navigation_step = any(_NAV_RE.search(t) for t in step_texts)
    is_field_focused = any(_FIELD_RE.search(t) for t in step_texts)
    expects_navigation_outcome = bool(
        _NAV_OUTCOME_RE.search(title or "")
        or any(_NAV_OUTCOME_RE.search(t) for t in step_texts)
    )

    targets: list[str] = []
    for p in clean_preconditions:
        for m in _TARGET_RE.finditer(p):
            token = re.sub(r"\s+", " ", m.group(1).strip().lower())
            if token and token not in targets:
                targets.append(token)

    required_data_keys: list[str] = []
    for s in clean_steps:
        key = str(s.get("data_input") or "").strip()
        if key and key not in required_data_keys:
            required_data_keys.append(key)

    needs_navigation_context = bool(is_field_focused and (has_navigation_precondition or has_navigation_step))

    contract = {
        "title": title,
        "authentication_required": authentication_required,
        "needs_navigation_context": needs_navigation_context,
        "expects_navigation_outcome": expects_navigation_outcome,
        "navigation_targets": targets,
        "required_data_keys": required_data_keys,
        "step_count": len(step_texts),
        "precondition_count": len(clean_preconditions),
    }
    logger.debug("build_generation_contract: %s", contract)
    return contract


def render_contract_for_prompt(contract: Optional[dict]) -> str:
    """Render structured contract as prompt text."""
    if not contract:
        return "(none)"

    targets = contract.get("navigation_targets") or []
    data_keys = contract.get("required_data_keys") or []
    return (
        f"- title: {contract.get('title') or ''}\n"
        f"- authentication_required: {bool(contract.get('authentication_required'))}\n"
        f"- needs_navigation_context: {bool(contract.get('needs_navigation_context'))}\n"
        f"- expects_navigation_outcome: {bool(contract.get('expects_navigation_outcome'))}\n"
        f"- navigation_targets: {', '.join(targets) if targets else '(none)'}\n"
        f"- required_data_keys: {', '.join(data_keys) if data_keys else '(none)'}\n"
        f"- step_count: {int(contract.get('step_count') or 0)}\n"
        f"- precondition_count: {int(contract.get('precondition_count') or 0)}"
    )


def validate_generated_script_navigation(contract: Optional[dict], code: str) -> list[str]:
    """
    Validate that generated code includes required setup/navigation evidence.

    Returns a list of issues. Empty means the contract is satisfied.
    """
    if not contract:
        return []

    issues: list[str] = []
    lower_code = (code or "").lower()

    if contract.get("authentication_required"):
        has_user = "get_data(\"username\"" in code or "get_data('username'" in code
        has_pass = "get_data(\"password\"" in code or "get_data('password'" in code
        if not has_user or not has_pass:
            issues.append(
                "Authentication precondition is present but script does not read both username and password via get_data()."
            )

    if contract.get("needs_navigation_context"):
        driver_get_count = code.count("driver.get(")
        has_navigation_action = ".click(" in code or driver_get_count > 1
        if not has_navigation_action:
            issues.append(
                "Scenario requires navigation context but script has no navigation action "
                "(for example click(...) or a second driver.get(...)) before assertions."
            )

        targets = contract.get("navigation_targets") or []
        if targets:
            if not any(t in lower_code for t in targets):
                issues.append(
                    "Scenario requires navigation context but script has no evidence of navigating to the required target screen."
                )

    if contract.get("expects_navigation_outcome"):
        has_transition_assert = any(
            token in lower_code
            for token in (
                "current_url",
                "url_contains",
                "url_changes",
                "url_to_be",
                "!= original_url",
            )
        )
        if not has_transition_assert:
            issues.append(
                "Scenario expects a navigation outcome (link/page transition) but script has no URL or route transition assertion."
            )

    if issues:
        logger.warning("validate_generated_script_navigation: %d issue(s)", len(issues))
        for issue in issues:
            logger.warning("  -> %s", issue)

    return issues
