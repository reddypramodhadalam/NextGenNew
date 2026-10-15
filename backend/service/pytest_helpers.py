"""
service/pytest_helpers.py

Single-responsibility module for all pytest scaffolding helpers.

Responsibilities:
  - Inject @pytest.mark severity decorators into generated test scripts
  - Generate shared conftest.py for WebDriver pooling across test suites
  - Read and timestamp the conftest template from backend/templates/

This module is intentionally separate from generation_runner.py (orchestration)
so that pytest scaffolding concerns don't leak into the generation pipeline.
"""

import datetime
import logging
import re
from pathlib import Path
from typing import Optional

from service.runner_utils import GENERATED_SCRIPTS_DIR

logger = logging.getLogger(__name__)

# Valid pytest mark severity levels
_VALID_SEVERITIES = ("smoke", "P0", "P1", "P2")

# Path to the conftest template file
_CONFTEST_TEMPLATE_PATH = (
    Path(__file__).parent.parent / "templates" / "conftest_template.py"
)


def inject_pytest_mark(script_content: str, severity: Optional[str] = None) -> str:
    """
    Inject a @pytest.mark decorator into a generated test script.

    Prepends the appropriate @pytest.mark decorator to the first test
    function definition found in the script. Severity levels:
        - 'smoke': Critical path, must pass before release
        - 'P0':    High priority, blocking other tests
        - 'P1':    Medium priority, normal test case (default)
        - 'P2':    Low priority, optional features

    Args:
        script_content: Generated Python test code.
        severity: One of 'smoke', 'P0', 'P1', 'P2'. Defaults to 'P1'.

    Returns:
        Script with @pytest.mark decorator injected before the first
        test function. Returns script unchanged if no test function found.

    Raises:
        ValueError: If script_content is not a string.
    """
    if not isinstance(script_content, str):
        raise ValueError(
            "inject_pytest_mark: script_content must be str, got %s"
            % type(script_content).__name__
        )

    if not severity:
        severity = "P1"
        logger.debug("inject_pytest_mark: no severity given, defaulting to P1")

    if severity not in _VALID_SEVERITIES:
        logger.warning(
            "inject_pytest_mark: invalid severity '%s', using P1", severity
        )
        severity = "P1"

    mark_decorator = "@pytest.mark.%s\n" % severity
    first_test_match = re.search(r"^def test_", script_content, flags=re.MULTILINE)
    if not first_test_match:
        logger.warning(
            "inject_pytest_mark: no 'def test_' found in script — returning unchanged"
        )
        return script_content

    # The model is instructed (see SCRIPT_PROMPT PYTEST MARKS section) to
    # choose its own single severity mark based on business importance. If
    # its choice differs from `severity` (the proposal's own priority tag),
    # the OLD check here — which only looked for the SAME severity already
    # present — missed it, and this function then prepended a SECOND,
    # different severity mark right above the first one:
    #     @pytest.mark.P0      <- written by the model
    #     @pytest.mark.P1      <- injected here, blindly, on top
    #     def test_...
    # Both decorators are syntactically legal, so nothing ever raised an
    # error — but the test silently carried two conflicting severities
    # (picked up by both `-m P0` and `-m P1`, double-counted in reporting).
    # Fix: find and strip ANY existing @pytest.mark.<severity> line
    # (severity, not just this exact one) that sits directly above the
    # first test function, before injecting the authoritative one below.
    prefix = script_content[:first_test_match.start()]
    remainder = script_content[first_test_match.start():]
    _severity_alternation = "|".join(re.escape(s) for s in _VALID_SEVERITIES)
    existing_marks = re.findall(
        rf"^[ \t]*@pytest\.mark\.(?:{_severity_alternation})[ \t]*\n",
        prefix,
        flags=re.MULTILINE,
    )
    if existing_marks:
        cleaned_prefix = re.sub(
            rf"^[ \t]*@pytest\.mark\.(?:{_severity_alternation})[ \t]*\n",
            "",
            prefix,
            flags=re.MULTILINE,
        )
        if len(existing_marks) > 1 or existing_marks[0].strip() != mark_decorator.strip():
            logger.info(
                "inject_pytest_mark: replacing existing mark(s) %s with @pytest.mark.%s",
                [m.strip() for m in existing_marks], severity,
            )
        else:
            logger.debug(
                "inject_pytest_mark: @pytest.mark.%s already present before first test; skipping",
                severity,
            )
            return script_content
        prefix = cleaned_prefix

    modified = prefix + mark_decorator + remainder
    logger.debug("inject_pytest_mark: injected @pytest.mark.%s", severity)
    return modified


def generate_conftest(output_dir: Optional[Path] = None) -> bool:
    """
    Generate a conftest.py for a test suite directory.

    Reads the Kavach conftest template, stamps it with the current
    timestamp, and writes it to the target directory. The generated file:
        - Provides a session-scoped WebDriver fixture (driver pooling)
        - Takes screenshots on test failure
        - Closes extra tabs after each test
        - Registers pytest markers (smoke, P0, P1, P2)

    Args:
        output_dir: Directory to write conftest.py into. Defaults to
                    GENERATED_SCRIPTS_DIR from runner_utils.

    Returns:
        True if conftest.py was written successfully, False otherwise.
        Failures are logged; no exception is raised.
    """
    if output_dir is None:
        output_dir = GENERATED_SCRIPTS_DIR
        logger.debug(
            "generate_conftest: output_dir not given, using GENERATED_SCRIPTS_DIR=%s",
            output_dir,
        )

    output_dir = Path(output_dir)

    if not _CONFTEST_TEMPLATE_PATH.exists():
        logger.error(
            "generate_conftest: conftest template not found at %s",
            _CONFTEST_TEMPLATE_PATH,
        )
        return False

    try:
        template_content = _CONFTEST_TEMPLATE_PATH.read_text(encoding="utf-8")

        # Inject generation timestamp
        now_iso = datetime.datetime.now().isoformat()
        content = template_content.replace("__GENERATED_DATE__", now_iso)

        output_dir.mkdir(parents=True, exist_ok=True)
        conftest_path = output_dir / "conftest.py"
        conftest_path.write_text(content, encoding="utf-8")

        logger.info(
            "generate_conftest: conftest.py written to %s", conftest_path
        )
        return True

    except Exception as exc:
        logger.error(
            "generate_conftest: failed to write conftest.py: %s", exc
        )
        return False
