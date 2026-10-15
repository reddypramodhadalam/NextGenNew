"""
agents/healer_agent.py

HEALER AGENT

Purpose:
    Given a broken selector and why it failed, finds the element that now
    serves the same purpose. Never applies its own proposal — always returns
    a proposal for human review and explicit approval.

Inputs:
    driver           — live Selenium WebDriver.
    test_case        — the original test case this selector belonged to.
    failed_selector  — the CSS selector that stopped matching.
    failure_notes    — why it broke.
    provider         — LLMProvider instance.
    on_step          — optional progress callback.

Output:
    {"found": bool, "role"?, "name"?, "confidence"?, "reasoning"?, "trace": [...]}
"""

import logging
from pathlib import Path
from typing import Callable, Optional

from llm.base import LLMProvider, LLMProviderError
from core import tools
from core.tools_registry import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

MAX_STEPS = 10
SYSTEM_PROMPT = (Path(__file__).parent.parent / "prompts" / "healer_system_prompt.txt").read_text(encoding="utf-8")

HEALER_TOOLS = [
    TOOL_DEFINITIONS["get_snapshot"],
    TOOL_DEFINITIONS["click"],
    TOOL_DEFINITIONS["done"],
    TOOL_DEFINITIONS["propose_replacement"],
]


def run_healer(
    driver,
    test_case: str,
    failed_selector: str,
    failure_notes: str,
    provider: LLMProvider,
    on_step: Optional[Callable] = None,
) -> dict:
    """
    Runs the Healer Agent loop.

    Args:
        driver: Live Selenium WebDriver.
        test_case: The original test case context.
        failed_selector: The selector that broke.
        failure_notes: Why it failed.
        provider: LLM provider from provider_factory.get_provider().
        on_step: Optional progress callback.

    Returns:
        On success: {"found": True, "role": ..., "name": ...,
                     "confidence": ..., "reasoning": ..., "trace": [...]}
        On failure: {"found": False, "notes": ..., "trace": [...]}
    """
    tool = provider.build_tool(HEALER_TOOLS)
    trace: list[dict] = []

    contents = [provider.user_text_content(
        f"Test case: {test_case}\n\n"
        f"Broken selector: {failed_selector}\n"
        f"Reason: {failure_notes}\n\n"
        "Find the current equivalent element and call propose_replacement, "
        "or call done if nothing plausibly matches."
    )]

    try:
        for step_num in range(1, MAX_STEPS + 1):
            response = provider.call_model(SYSTEM_PROMPT, tool, contents)
            name, args = provider.extract_function_call(response)

            if name is None:
                step = {"action": "done", "input": {"passed": False, "notes": "No tool call"}}
                trace.append(step)
                if on_step:
                    on_step(step_num, step)
                return {"found": False, "notes": "Model responded without a tool call.", "trace": trace}

            contents.append(response.candidates[0].content)

            if name == "propose_replacement":
                step = {"action": name, "input": args}
                trace.append(step)
                if on_step:
                    on_step(step_num, step)
                return {
                    "found": True,
                    "role": args["role"],
                    "name": args["name"],
                    "confidence": args["confidence"],
                    "reasoning": args["reasoning"],
                    "trace": trace,
                }

            if name == "get_snapshot":
                result = tools.get_snapshot(driver)
            elif name == "click":
                result = tools.click(args["ref"])
            elif name == "done":
                result = tools.done(args.get("passed", False), args.get("notes", ""))
            else:
                result = {"error": f"Unknown tool: {name}"}

            step = {"action": name, "input": args}
            trace.append(step)
            if on_step:
                on_step(step_num, step)

            if name == "done":
                return {"found": False, "notes": args.get("notes", ""), "trace": trace}

            contents.append(provider.function_response_content(name, result))

        return {"found": False, "notes": f"Gave up after {MAX_STEPS} steps", "trace": trace}

    except LLMProviderError as e:
        logger.error("Healer agent LLM failure: %s", e)
        return {"found": False, "notes": f"LLM provider error: {e}", "trace": trace}

    except Exception as e:
        logger.exception("Unexpected error in Healer agent")
        return {"found": False, "notes": f"Unexpected error: {e}", "trace": trace}
