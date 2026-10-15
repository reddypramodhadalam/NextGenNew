"""
service/step_validator.py

Handles:
  - Detection of unautomatable steps (matched: false flag)
  - Annotation of steps with [NOT AUTOMATED] prefix
  - Reason extraction and display
  - Validation and counting of unautomatable steps

This module is part of the test generation pipeline and ensures that
steps without matched elements are properly marked so the LLM skips them.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class UnautomatableStepError(Exception):
    """Raised when a step cannot be automated."""
    pass


def is_step_automatable(step: dict) -> bool:
    """
    Determine if a step can be automated based on the matched flag.
    
    Args:
        step: Step dict with optional 'matched' key (default: True)
        
    Returns:
        True if step can be automated, False otherwise.
        
    Raises:
        ValueError: If step is not a dict.
    """
    if not isinstance(step, dict):
        raise ValueError(f"Expected dict, got {type(step).__name__}")
    
    # Default to True for backward compatibility (older surveys don't have matched)
    matched = step.get("matched", True)
    
    if not isinstance(matched, bool):
        logger.warning(
            "is_step_automatable: step matched field not bool, got %s — treating as True",
            type(matched).__name__
        )
        return True
    
    return matched


def get_match_reason(step: dict) -> str:
    """
    Extract the reason why a step is unautomatable.
    
    Args:
        step: Step dict with optional 'match_reason' key
        
    Returns:
        Reason string, or default message if not present.
    """
    reason = step.get("match_reason", "element not found")
    
    if not isinstance(reason, str):
        reason = str(reason)
    
    # Truncate very long reasons
    if len(reason) > 100:
        reason = reason[:97] + "..."
    
    return reason.strip()


def annotate_step(step: dict, index: int) -> tuple[str, bool]:
    """
    Render a step with [NOT AUTOMATED] prefix if unautomatable.
    
    Args:
        step: Step dict with keys 'step', 'expected', 'matched', 'match_reason'
        index: Step number (1-based) for logging
        
    Returns:
        Tuple of (annotated_text, is_automatable)
        where annotated_text is the [NOT AUTOMATED] prefixed text if needed
        
    Raises:
        ValueError: If step is not a dict or missing required keys.
    """
    if not isinstance(step, dict):
        raise ValueError(f"Step must be dict, got {type(step).__name__}")
    
    step_text = (step.get("step") or "").strip()
    expected = (step.get("expected") or "").strip()
    
    if not step_text:
        logger.warning("annotate_step: step %d has no text", index)
    
    # Build base line
    line = f"{index}. {step_text}" if step_text else f"{index}."
    
    # Check if automatable
    automatable = is_step_automatable(step)
    
    if not automatable:
        # Prepend [NOT AUTOMATED] marker
        reason = get_match_reason(step)
        line = f"{index}. [NOT AUTOMATED] {step_text}"
        if reason:
            line += f" ({reason})"
        logger.debug(
            "annotate_step: step %d marked unautomatable — reason: %s",
            index, reason
        )
    
    # Append expected result (same for both automatable and unautomatable)
    if expected:
        line += f"  →  {expected}"
    
    return line, automatable


def count_unautomatable_steps(steps: Optional[list[dict]]) -> tuple[int, int]:
    """
    Count automatable and unautomatable steps in a list.
    
    Args:
        steps: List of step dicts, or None
        
    Returns:
        Tuple of (total, unautomatable_count)
    """
    if not steps:
        return 0, 0
    
    unautomatable = sum(1 for step in steps if not is_step_automatable(step))
    return len(steps), unautomatable


def validate_unautomatable_coverage(
    steps: Optional[list[dict]],
    generated_code: str,
    min_manual_threshold: int = 0
) -> dict:
    """
    Validate that generated code properly handles unautomatable steps.
    
    Checks:
      - Count of [NOT AUTOMATED] in flattened steps
      - Count of # MANUAL comments in generated code
      - Counts should match (with optional threshold tolerance)
    
    Args:
        steps: Original step list
        generated_code: Generated Python script
        min_manual_threshold: Minimum acceptable # MANUAL comments
                             (default 0: no validation)
    
    Returns:
        Validation result dict:
        {
            "valid": bool,
            "total_steps": int,
            "unautomatable_steps": int,
            "manual_comments_in_code": int,
            "matches": bool,
            "message": str
        }
    """
    total, unautomatable = count_unautomatable_steps(steps)
    
    # Count # MANUAL comments in generated code
    manual_count = generated_code.count("# MANUAL:")
    
    # Check if counts match
    matches = manual_count >= unautomatable
    
    is_valid = matches and manual_count >= min_manual_threshold
    
    message = (
        f"Found {unautomatable} unautomatable steps (total {total}), "
        f"{manual_count} MANUAL comments in generated code. "
        f"Match: {'✓' if matches else '✗'}"
    )
    
    result = {
        "valid": is_valid,
        "total_steps": total,
        "unautomatable_steps": unautomatable,
        "manual_comments_in_code": manual_count,
        "matches": matches,
        "message": message,
    }
    
    log_level = logging.INFO if is_valid else logging.WARNING
    logger.log(log_level, "validate_unautomatable_coverage: %s", message)
    
    return result
