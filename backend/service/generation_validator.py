"""
service/generation_validator.py

Validation module for Phase 3-A: Generation consistency checks.

Handles:
  - Validation that generated code matches survey expectations
  - Unautomatable step coverage verification
  - Generation quality metrics and logging
"""

import logging
import re
from typing import Optional

from service.step_validator import count_unautomatable_steps

logger = logging.getLogger(__name__)


def validate_generation_against_steps(
    steps: Optional[list[dict]],
    generated_code: str,
    proposal_id: str,
    verbose: bool = False,
) -> dict:
    """
    Validate that generated code properly reflects the proposal steps.
    
    Checks:
      1. Code contains function definition (def test_*)
      2. Unautomatable steps have corresponding # MANUAL comments
      3. Code contains logging for skipped steps
    
    Args:
        steps: Original steps from proposal
        generated_code: Python test script
        proposal_id: ID for logging context
        verbose: Log all validations, not just errors
        
    Returns:
        Validation result dict:
        {
            "valid": bool,
            "proposal_id": str,
            "total_steps": int,
            "unautomatable_steps": int,
            "manual_comments": int,
            "has_function": bool,
            "has_logging": bool,
            "issues": [str],
            "message": str,
        }
    """
    issues = []
    total, unautomatable = count_unautomatable_steps(steps)
    
    # Check 1: Function definition
    has_function = bool(re.search(r"def test_\w+\(", generated_code))
    if not has_function:
        issues.append("Generated code has no test function definition (def test_*)")
    
    # Check 2: MANUAL comments for unautomatable steps
    manual_comments = generated_code.count("# MANUAL:")
    if unautomatable > 0 and manual_comments < unautomatable:
        issues.append(
            f"Found {unautomatable} unautomatable steps but only {manual_comments} "
            f"# MANUAL comments in code (expected at least {unautomatable})"
        )
    
    # Check 3: Logging for skipped steps
    has_logging = "logger" in generated_code.lower()
    if unautomatable > 0 and not has_logging:
        issues.append(
            f"Code has {unautomatable} unautomatable steps but no logging for skipped steps"
        )
    
    is_valid = len(issues) == 0 and has_function
    
    result = {
        "valid": is_valid,
        "proposal_id": proposal_id,
        "total_steps": total,
        "unautomatable_steps": unautomatable,
        "manual_comments": manual_comments,
        "has_function": has_function,
        "has_logging": has_logging,
        "issues": issues,
        "message": (
            f"Proposal {proposal_id}: {total} steps ({unautomatable} unautomatable), "
            f"{manual_comments} manual comments, {len(issues)} issue(s)"
        ),
    }
    
    if verbose or issues:
        log_level = logging.WARNING if issues else logging.DEBUG
        logger.log(log_level, "validate_generation_against_steps: %s", result["message"])
        for issue in issues:
            logger.warning("  → %s", issue)
    
    return result


def log_generation_summary(
    proposal_id: str,
    steps: Optional[list[dict]],
    generated_code: str,
) -> None:
    """
    Log a summary of the generation result.
    
    Args:
        proposal_id: Proposal ID
        steps: Steps from proposal
        generated_code: Generated code
    """
    total, unautomatable = count_unautomatable_steps(steps)
    manual_count = generated_code.count("# MANUAL:")
    function_count = len(re.findall(r"def test_\w+\(", generated_code))
    loc = len(generated_code.splitlines())
    
    logger.info(
        "Generation complete: proposal=%s  steps=%d (unautomatable=%d) "
        "manual=%d  functions=%d  loc=%d",
        proposal_id, total, unautomatable, manual_count, function_count, loc,
    )
