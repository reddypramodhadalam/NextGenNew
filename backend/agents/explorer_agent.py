"""
agents/explorer_agent.py

EXPLORER AGENT

Purpose:
    Given a live browser session and one plain-English test case, carries
    it out by reading the screen, deciding what to interact with, and
    verifying the expected outcome. Produces a trace that the compiler
    turns into a saved, reusable Selenium/pytest script.

Inputs:
    driver       — a live Selenium WebDriver, already on the target page.
    test_case    — plain-English description of what to verify.
    provider     — an LLMProvider instance (from llm/provider_factory.py).
    on_step      — optional callback(step_num, step_dict) for live progress.

Output:
    A list of trace steps ending in a "done" step with passed/notes/source.
    source="model" means the agent reached a real verdict.
    source="system" means the agent gave up (quota, timeout, error) —
    this distinction is critical: system give-ups must never be compiled
    or reported as genuine test failures.
"""

import logging
from pathlib import Path
from typing import Callable, Optional

from llm.base import LLMProvider, LLMProviderError
from core import tools
from core.tools_registry import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

MAX_STEPS = 20
SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "explorer_system_prompt.txt").read_text(encoding="utf-8")

EXPLORER_TOOLS = [
    TOOL_DEFINITIONS["get_snapshot"],
    TOOL_DEFINITIONS["click"],
    TOOL_DEFINITIONS["type_text"],
    TOOL_DEFINITIONS["done"],
]


def run_explorer(
    driver,
    test_case: str,
    provider: LLMProvider,
    on_step: Optional[Callable] = None,
) -> list[dict]:
    """
    Runs the Explorer Agent loop.

    Args:
        driver: A live Selenium WebDriver instance, already on the right page.
        test_case: Plain-English description of what to carry out.
        provider: LLM provider instance from provider_factory.get_provider().
        on_step: Optional callback called after every step with
            (step_number: int, step: dict). Used for live progress streaming.

    Returns:
        A trace: a list of step dicts. The last step is always a "done" step.
        Check step["input"]["source"] to distinguish a real model verdict
        from a system give-up.
    """
    tool = provider.build_tool(EXPLORER_TOOLS)
    trace: list[dict] = []
    last_snapshot_by_ref: dict[str, dict] = {}

    contents = [provider.user_text_content(
        f"Test case: {test_case}\n\nExplore the current screen and carry out this test case."
    )]

    try:
        for step_num in range(1, MAX_STEPS + 1):
            response = provider.call_model(SYSTEM_PROMPT, tool, contents)
            name, args = provider.extract_function_call(response)

            if name is None:
                step = _system_done("Model responded without a tool call.")
                trace.append(step)
                if on_step:
                    on_step(step_num, step)
                return trace

            contents.append(response.candidates[0].content)
            result = _execute_tool(driver, name, args, last_snapshot_by_ref)

            step = {"action": name, "input": args}
            if name in ("click", "type_text"):
                step["ref_info"] = last_snapshot_by_ref.get(args.get("ref"), {})
            if name == "done":
                # This is a real model verdict — tag it so the compiler knows.
                step["input"] = {**step["input"], "source": "model"}

            trace.append(step)
            if on_step:
                on_step(step_num, step)

            contents.append(provider.function_response_content(name, result))

            if name == "done":
                return trace

        # Ran out of steps without calling done
        step = _system_done(f"Gave up after {MAX_STEPS} steps without reaching a verdict.")
        trace.append(step)
        if on_step:
            on_step(MAX_STEPS, step)
        return trace

    except LLMProviderError as e:
        # Provider failed after all retries — this is NOT a real test result.
        logger.error("Explorer agent LLM failure: %s", e)
        step = _system_done(f"LLM provider error: {e}")
        trace.append(step)
        if on_step:
            on_step(len(trace), step)
        return trace

    except Exception as e:
        # Catch-all so callers always get a well-formed trace, never a crash.
        logger.exception("Unexpected error in Explorer agent")
        step = _system_done(f"Unexpected error: {e}")
        trace.append(step)
        if on_step:
            on_step(len(trace), step)
        return trace


def _execute_tool(
    driver, name: str, args: dict, last_snapshot_by_ref: dict
) -> object:
    """Dispatches a tool call to the appropriate core/tools function."""
    if name == "get_snapshot":
        snapshot = tools.get_snapshot(driver)
        last_snapshot_by_ref.clear()
        last_snapshot_by_ref.update({
            el["ref"]: el for el in snapshot if "ref" in el
        })
        return snapshot
    if name == "click":
        return tools.click(args["ref"])
    if name == "type_text":
        return tools.type_text(args["ref"], args["text"])
    if name == "done":
        return tools.done(args.get("passed", False), args.get("notes", ""))
    return {"error": f"Unknown tool: {name}"}


def _system_done(notes: str) -> dict:
    """
    Creates a system-level done step (not a real model verdict).
    source="system" tells the compiler this is an infrastructure failure,
    not a genuine test result — it must never become an assert False.
    """
    return {
        "action": "done",
        "input": {"passed": False, "notes": notes, "source": "system"},
    }
