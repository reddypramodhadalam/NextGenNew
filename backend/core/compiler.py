"""
core/compiler.py

Converts an Explorer Agent trace into a saved, reusable Selenium/pytest
script. Pure, deterministic conversion — no LLM calls.

Key rule: system give-ups (source="system") compile to pytest.skip(),
not assert False. This is the structural fix for a real bug found during
development: a Gemini quota outage must never be compiled as if the model
verified that a feature is broken.
"""

import logging

logger = logging.getLogger(__name__)


def compile_trace(trace: list[dict], test_name: str, base_url: str) -> str:
    """
    Converts a trace into a complete, self-contained pytest/Selenium file.

    Args:
        trace: The list of step dicts from run_explorer().
        test_name: The test function name (spaces and hyphens → underscores).
        base_url: The target URL, used as the default BASE_URL in the script.

    Returns:
        A string containing the full Python script, ready to write to disk.
    """
    safe_name = test_name.replace(" ", "_").replace("-", "_")

    needs_pytest = any(
        step["action"] == "done"
        and step["input"].get("source", "system") == "system"
        for step in trace
    )

    lines = ["import os"]
    if needs_pytest:
        lines.append("import pytest")
    lines += [
        "from selenium import webdriver",
        "from selenium.webdriver.chrome.options import Options",
        "from selenium.webdriver.common.by import By",
        "",
        f'BASE_URL = os.environ.get("NEXTGEN2_BASE_URL", "{base_url}")',
        "",
        "",
        f"def test_{safe_name}():",
        "    options = Options()",
        '    options.add_argument("--headless=new")',
        "    driver = webdriver.Chrome(options=options)",
        "    try:",
        "        driver.get(BASE_URL)",
    ]

    for step in trace:
        action = step["action"]
        inp = step["input"]

        if action == "click":
            ref_info = step.get("ref_info", {})
            selector = _build_selector(ref_info)
            lines.append(f'        driver.find_element(By.CSS_SELECTOR, "{selector}").click()')

        elif action == "type_text":
            ref_info = step.get("ref_info", {})
            selector = _build_selector(ref_info)
            text = _escape(inp.get("text", ""))
            lines.append(f'        el = driver.find_element(By.CSS_SELECTOR, "{selector}")')
            lines.append("        el.clear()")
            lines.append(f"        el.send_keys('{text}')")

        elif action == "done":
            passed = inp.get("passed", False)
            notes = _escape(inp.get("notes", ""))
            source = inp.get("source", "system")

            if source == "model":
                status = "PASSED" if passed else "FAILED"
                lines.append(f"        # Agent verdict: {status} — {notes}")
                if not passed:
                    lines.append(f'        assert False, "{notes}"')
            else:
                # source="system" — infrastructure failure, not a real verdict.
                lines.append(f"        # Generation did not complete: {notes}")
                lines.append(
                    f'        pytest.skip("Generation incomplete ({notes}) — '
                    f'regenerate before trusting this test.")'
                )

    lines += [
        "    finally:",
        "        driver.quit()",
    ]

    return "\n".join(lines)


def _build_selector(ref_info: dict) -> str:
    """Builds a CSS selector from the ref_info captured at exploration time."""
    role = ref_info.get("role", "")
    name = _escape(ref_info.get("name", ""))
    real_tags = {"button", "a", "input", "textarea", "select"}

    if role in real_tags:
        return f"{role}[aria-label='{name}'], {role}[placeholder='{name}']"
    return f"[aria-label='{name}']"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
