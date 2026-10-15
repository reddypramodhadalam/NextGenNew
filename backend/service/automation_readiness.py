"""
service/automation_readiness.py

Validation helpers for deciding whether an accepted scenario is ready for
automation generation.

Design goal:
  - Keep acceptance non-breaking: a scenario can still be accepted and stored
    even when automation is unsafe.
  - Block only clearly non-runnable automation requests.
  - Return structured reasons/warnings for audit visibility.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


_ACTION_RE = re.compile(
    r"\b(click|tap|type|enter|select|choose|submit|open|close|search|"
    r"filter|upload|navigate|go to|press)\b",
    re.IGNORECASE,
)

_NAVIGATION_RE = re.compile(
    r"\b(navigate|go to|open|menu|tab|screen|page|home|dashboard|settings|"
    r"profile|route|url)\b",
    re.IGNORECASE,
)

_SCREEN_PRECONDITION_RE = re.compile(
    r"\b(user is on|on the .* (page|screen)|starting screen|home screen|"
    r"authenticated|logged in)\b",
    re.IGNORECASE,
)

_FIELD_FOCUSED_RE = re.compile(
    r"\b(field|input|textbox|text box|dropdown|drop-down|form|enter|type|"
    r"value|placeholder)\b",
    re.IGNORECASE,
)


def evaluate_automation_readiness(
    *,
    title: str,
    steps: Optional[list[dict]],
    preconditions: Optional[list],
    requested: bool,
) -> dict:
    """
    Evaluate whether a scenario should proceed to script generation.

    Args:
        title: Scenario title for logs.
        steps: Structured steps from accepted proposal.
        preconditions: Preconditions from accepted proposal.
        requested: Whether the caller asked for automation.

    Returns:
        Dict with eligibility decision and reasons/warnings.
    """
    clean_steps = [s for s in (steps or []) if isinstance(s, dict) and str(s.get("step") or "").strip()]
    clean_preconditions = [str(p).strip() for p in (preconditions or []) if str(p).strip()]

    blocked_reasons: list[str] = []
    warnings: list[str] = []

    if not requested:
        return {
            "requested": False,
            "eligible_for_automation": False,
            "blocked_reasons": [],
            "warnings": [],
            "metrics": {
                "step_count": len(clean_steps),
                "precondition_count": len(clean_preconditions),
            },
        }

    if not clean_steps:
        blocked_reasons.append("No structured steps were provided for this scenario.")

    actionable_steps = [s for s in clean_steps if _ACTION_RE.search(str(s.get("step") or ""))]
    if clean_steps and not actionable_steps:
        blocked_reasons.append("No actionable interaction step was detected.")

    missing_expected = [
        idx for idx, s in enumerate(clean_steps, 1)
        if not str(s.get("expected") or "").strip()
    ]
    if missing_expected:
        warnings.append(
            "Some steps have no expected result: "
            + ", ".join(str(i) for i in missing_expected[:8])
            + ("..." if len(missing_expected) > 8 else "")
        )

    has_navigation_precondition = any(_SCREEN_PRECONDITION_RE.search(p) for p in clean_preconditions)
    has_navigation_step = any(_NAVIGATION_RE.search(str(s.get("step") or "")) for s in clean_steps)
    is_field_focused = any(_FIELD_FOCUSED_RE.search(str(s.get("step") or "")) for s in clean_steps)

    # Block only the clear failure mode we have observed repeatedly:
    # field-level validation steps with no route/screen context.
    if is_field_focused and not has_navigation_precondition and not has_navigation_step:
        blocked_reasons.append(
            "Field-focused scenario has no navigation or starting-screen context. "
            "Add a starting screen precondition or explicit navigation step."
        )

    eligible = len(blocked_reasons) == 0

    result = {
        "requested": True,
        "eligible_for_automation": eligible,
        "blocked_reasons": blocked_reasons,
        "warnings": warnings,
        "metrics": {
            "step_count": len(clean_steps),
            "actionable_step_count": len(actionable_steps),
            "precondition_count": len(clean_preconditions),
            "missing_expected_count": len(missing_expected),
            "has_navigation_precondition": has_navigation_precondition,
            "has_navigation_step": has_navigation_step,
            "is_field_focused": is_field_focused,
        },
    }

    if eligible:
        logger.info(
            "automation_readiness: eligible title=%r steps=%d preconditions=%d",
            title[:120],
            len(clean_steps),
            len(clean_preconditions),
        )
    else:
        logger.warning(
            "automation_readiness: blocked title=%r reasons=%s",
            title[:120],
            "; ".join(blocked_reasons),
        )

    return result
