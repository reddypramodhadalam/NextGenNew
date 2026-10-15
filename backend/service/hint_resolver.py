"""
service/hint_resolver.py

Handles:
  - Extracting element hints from steps
  - Formatting hints for LLM clarity
  - Generating CSS selectors from hints (optional)
  - Selector validation and testing
"""

import logging
import re
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)


def extract_hint(step: dict) -> Optional[str]:
    """
    Extract and normalize element hint from a step.
    
    Args:
        step: Step dict potentially containing element_hint field
        
    Returns:
        Normalized hint string (e.g., "aria-label='Settings'") or None
        
    Raises:
        ValueError: If step is not a dict or hint format is invalid
    """
    if not isinstance(step, dict):
        raise ValueError(f"Expected dict, got {type(step).__name__}")
    
    hint = step.get("element_hint")
    if not hint:
        logger.debug("extract_hint: no element_hint in step")
        return None
    
    if not isinstance(hint, str):
        logger.warning("extract_hint: element_hint not string, got %s", type(hint).__name__)
        return None
    
    hint = hint.strip()
    if not hint:
        logger.debug("extract_hint: element_hint is empty string")
        return None
    
    logger.debug("extract_hint: extracted hint=%s", hint)
    return hint


def format_hint_for_step(hint: Optional[str], max_length: int = 60) -> str:
    """
    Format a hint for inclusion in step text.
    
    Args:
        hint: Raw hint string or None
        max_length: Maximum formatted hint length
        
    Returns:
        Formatted hint like "[aria-label='Settings']" or ""
    """
    if not hint:
        return ""
    
    if len(hint) > max_length:
        # Truncate long hints gracefully
        hint = hint[:max_length-3] + "..."
        logger.debug("format_hint_for_step: truncated hint to %d chars", max_length)
    
    formatted = f" [{hint}]"
    logger.debug("format_hint_for_step: formatted_hint=%s", formatted)
    return formatted


def parse_hint_attribute(hint: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse hint into attribute and value.
    
    Args:
        hint: Hint string like "aria-label='Settings'" or "name='username'"
        
    Returns:
        (attribute, value) tuple or (None, None) if unparseable
        
    Raises:
        ValueError: If hint format is invalid
    """
    if not isinstance(hint, str):
        raise ValueError(f"Expected string, got {type(hint).__name__}")
    
    # Pattern: attr='value' or attr="value"
    match = re.match(r"(\w[\w\-]*)\s*=\s*['\"]([^'\"]*)['\"]", hint.strip())
    
    if not match:
        logger.warning("parse_hint_attribute: could not parse hint=%s", hint)
        return None, None
    
    attr, value = match.groups()
    logger.debug("parse_hint_attribute: attr=%s  value=%s", attr, value)
    return attr, value


def hint_to_selector(hint: str, tag: Optional[str] = None) -> Optional[str]:
    """
    Convert a hint to a CSS selector.
    
    Args:
        hint: Hint like "aria-label='Settings'" or "name='username'"
        tag: Optional tag to restrict selector (e.g., "button", "input")
        
    Returns:
        CSS selector like "button[aria-label='Settings']" or None
        
    Raises:
        ValueError: If hint format invalid
    """
    attr, value = parse_hint_attribute(hint)
    if not attr or not value:
        logger.warning("hint_to_selector: could not parse hint=%s", hint)
        return None
    
    # Special handling for certain attributes
    if attr == "id":
        selector = f"#{value}"
        logger.debug("hint_to_selector: id hint → selector=%s", selector)
        return selector
    
    if attr == "class":
        # Handle class names carefully (can have spaces)
        classes = ".".join(value.split())
        selector = f".{classes}" if classes else None
        logger.debug("hint_to_selector: class hint → selector=%s", selector)
        return selector
    
    if attr == "name":
        # Name hints often for input/textarea
        selector = f"input[{attr}='{value}'], textarea[{attr}='{value}'], select[{attr}='{value}']"
        logger.debug("hint_to_selector: name hint → selector=%s", selector)
        return selector
    
    if attr == "aria-label":
        # aria-label hints for buttons, links, etc.
        # If tag specified, use it; otherwise be flexible
        if tag:
            selector = f"{tag}[{attr}='{value}']"
        else:
            selector = f"[{attr}='{value}']"
        logger.debug("hint_to_selector: aria-label hint → selector=%s", selector)
        return selector
    
    # Generic: just build the attribute selector
    selector = f"[{attr}='{value}']"
    if tag:
        selector = f"{tag}{selector}"
    logger.debug("hint_to_selector: generic hint → selector=%s", selector)
    return selector


def validate_selector_from_hint(selector: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that a selector is well-formed CSS.
    
    Args:
        selector: CSS selector to validate
        
    Returns:
        (is_valid, error_message) tuple
        
    Raises:
        ValueError: If selector not a string
    """
    if not isinstance(selector, str):
        raise ValueError(f"Expected string, got {type(selector).__name__}")
    
    if not selector.strip():
        return False, "Selector is empty"
    
    # Basic validation: check for balanced brackets
    if selector.count("[") != selector.count("]"):
        logger.warning("validate_selector_from_hint: unbalanced brackets in %s", selector)
        return False, "Unbalanced brackets in selector"
    
    if selector.count("(") != selector.count(")"):
        logger.warning("validate_selector_from_hint: unbalanced parens in %s", selector)
        return False, "Unbalanced parentheses in selector"
    
    # Check for unclosed quotes
    single_quotes = selector.count("'") % 2
    double_quotes = selector.count('"') % 2
    if single_quotes != 0 or double_quotes != 0:
        logger.warning("validate_selector_from_hint: unclosed quotes in %s", selector)
        return False, "Unclosed quotes in selector"
    
    logger.debug("validate_selector_from_hint: selector valid: %s", selector)
    return True, None


def get_hint_confidence(hint: str) -> float:
    """
    Rate how confident we should be in a hint for selector generation.
    
    Args:
        hint: Hint string to rate
        
    Returns:
        Confidence score 0.0-1.0
        
    Raises:
        ValueError: If hint not a string
    """
    if not isinstance(hint, str):
        raise ValueError(f"Expected string, got {type(hint).__name__}")
    
    score = 0.5  # Base confidence
    
    # High-confidence attributes
    if "id=" in hint:
        score = 0.95
        logger.debug("get_hint_confidence: id hint → high confidence")
    elif "data-test" in hint or "data-testid" in hint:
        score = 0.9
        logger.debug("get_hint_confidence: data-test hint → high confidence")
    elif "aria-label=" in hint or "name=" in hint:
        score = 0.8
        logger.debug("get_hint_confidence: aria-label/name hint → medium-high confidence")
    elif "placeholder=" in hint:
        score = 0.7
        logger.debug("get_hint_confidence: placeholder hint → medium confidence")
    elif "class=" in hint:
        score = 0.6
        logger.debug("get_hint_confidence: class hint → medium-low confidence")
    else:
        score = 0.5
        logger.debug("get_hint_confidence: generic hint → low confidence")
    
    return score


def annotate_hint(hint: Optional[str], matched: bool) -> str:
    """
    Annotate a hint based on step match status.
    
    Args:
        hint: Element hint or None
        matched: Whether step's element was found
        
    Returns:
        Annotated hint string for step text
    """
    if not hint:
        return ""
    
    if not matched:
        # Unmatched steps: hint is informational only
        return f" [{hint} — not found on page]"
    else:
        # Matched steps: hint is reliable selector
        return f" [{hint}]"


def collect_hints_from_steps(steps: Optional[list[dict]]) -> Dict[int, str]:
    """
    Collect all hints from a set of steps.
    
    Args:
        steps: List of step dicts
        
    Returns:
        Dict mapping step index → hint string
    """
    hints = {}
    
    if not steps:
        logger.debug("collect_hints_from_steps: no steps provided")
        return hints
    
    for i, step in enumerate(steps):
        try:
            hint = extract_hint(step)
            if hint:
                hints[i] = hint
                logger.debug("collect_hints_from_steps: step %d has hint=%s", i, hint)
        except Exception as e:
            logger.warning("collect_hints_from_steps: error on step %d: %s", i, e)
    
    logger.info("collect_hints_from_steps: collected %d hints from %d steps",
                len(hints), len(steps))
    return hints


def validate_hint_coverage(steps: Optional[list[dict]], min_coverage: float = 0.8) -> Dict[str, Any]:
    """
    Validate that most steps have element hints.
    
    Args:
        steps: List of step dicts
        min_coverage: Minimum fraction of steps that should have hints (0.0-1.0)
        
    Returns:
        Validation result dict:
        {
            "valid": bool,
            "total_steps": int,
            "steps_with_hints": int,
            "coverage_percent": float,
            "message": str,
        }
    """
    if not steps:
        logger.debug("validate_hint_coverage: no steps")
        return {
            "valid": True,
            "total_steps": 0,
            "steps_with_hints": 0,
            "coverage_percent": 0.0,
            "message": "No steps to validate",
        }
    
    hints = collect_hints_from_steps(steps)
    total = len(steps)
    with_hints = len(hints)
    coverage = with_hints / total if total > 0 else 0.0
    is_valid = coverage >= min_coverage
    
    result = {
        "valid": is_valid,
        "total_steps": total,
        "steps_with_hints": with_hints,
        "coverage_percent": round(coverage * 100, 1),
        "message": f"Hint coverage: {with_hints}/{total} steps ({coverage*100:.1f}%)",
    }
    
    if not is_valid:
        logger.warning("validate_hint_coverage: %s", result["message"])
    else:
        logger.info("validate_hint_coverage: %s", result["message"])
    
    return result
