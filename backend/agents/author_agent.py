"""
agents/author_agent.py

AUTHOR AGENT

Purpose:
    Given only a URL (and optionally standards/requirements context from
    the KnowledgeService), surveys the screen and proposes plausible
    test cases. Has no ground truth to verify against — everything it
    produces is a proposal for human review, never treated as verified.

Inputs:
    driver       — live Selenium WebDriver, already on the target page.
    provider     — LLMProvider instance.
    context      — optional string of relevant standards/requirements text
                   retrieved by the KnowledgeService, prepended to the prompt.
    on_step      — optional progress callback.

Output:
    dict with keys:
        test_cases: list[str]  — proposed test case descriptions.
        trace: list[dict]      — full step trace for audit.
"""

import logging
from pathlib import Path
from typing import Callable, Optional

from llm.base import LLMProvider, LLMProviderError
from core import tools
from core.tools_registry import TOOL_DEFINITIONS

logger = logging.getLogger(__name__)

MAX_STEPS = 12
_BASE_PROMPT = (Path(__file__).parent.parent / "prompts" / "author_system_prompt.txt").read_text(encoding="utf-8")

AUTHOR_TOOLS = [
    TOOL_DEFINITIONS["get_snapshot"],
    TOOL_DEFINITIONS["click"],
    TOOL_DEFINITIONS["propose_test_cases"],
]


def run_author(
    driver,
    provider: LLMProvider,
    context: Optional[str] = None,
    on_step: Optional[Callable] = None,
) -> dict:
    """
    Runs the Author Agent loop.

    Args:
        driver: Live Selenium WebDriver instance.
        provider: LLM provider from provider_factory.get_provider().
        context: Optional standards/requirements text to ground generation.
            If provided, it is prepended to the system prompt so the agent
            knows what business rules to look for negative cases around.
        on_step: Optional callback(step_number, step_dict).

    Returns:
        {"test_cases": [...], "trace": [...]}
    """
    system_prompt = _BASE_PROMPT
    if context:
        system_prompt = f"RELEVANT STANDARDS AND REQUIREMENTS:\n{context}\n\n{_BASE_PROMPT}"

    tool = provider.build_tool(AUTHOR_TOOLS)
    trace: list[dict] = []

    contents = [provider.user_text_content(
        "Survey this screen and propose test cases worth automating."
    )]

    try:
        for step_num in range(1, MAX_STEPS + 1):
            response = provider.call_model(system_prompt, tool, contents)
            name, args = provider.extract_function_call(response)

            if name is None:
                step = {"action": "propose_test_cases", "input": {"test_cases": []}}
                trace.append(step)
                if on_step:
                    on_step(step_num, step)
                logger.warning("Author agent: model returned no tool call at step %d", step_num)
                return {"test_cases": [], "trace": trace}

            contents.append(response.candidates[0].content)

            if name == "propose_test_cases":
                step = {"action": name, "input": args}
                trace.append(step)
                if on_step:
                    on_step(step_num, step)
                return {"test_cases": args.get("test_cases", []), "trace": trace}

            if name == "get_snapshot":
                result = tools.get_snapshot(driver)
            elif name == "click":
                result = tools.click(args["ref"])
            else:
                result = {"error": f"Unknown tool: {name}"}

            step = {"action": name, "input": args}
            trace.append(step)
            if on_step:
                on_step(step_num, step)

            contents.append(provider.function_response_content(name, result))

        logger.warning("Author agent: reached max steps without proposing")
        return {"test_cases": [], "trace": trace}

    except LLMProviderError as e:
        logger.error("Author agent LLM failure: %s", e)
        return {"test_cases": [], "trace": trace}

    except Exception as e:
        logger.exception("Unexpected error in Author agent")
        return {"test_cases": [], "trace": trace}
