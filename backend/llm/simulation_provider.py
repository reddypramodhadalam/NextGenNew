"""
llm/simulation_provider.py

A provider that makes NO real LLM calls. Lets the full
survey -> generate -> execute pipeline run with no API key, producing
realistic canned output — for demos and when the real key is down.

IMPORTANT: the agents (Author/Explorer) were written against Gemini's
response shape and do  contents.append(response.candidates[0].content)
BEFORE inspecting the tool call. So this provider's response object
must expose a compatible `.candidates[0].content`. That is provided
below by _SimResponse / _SimCandidate / _SimContent — without it the
Author agent crashes with 'no attribute candidates' and produces 0
scenarios.

Behaviour per agent (detected from the tools passed in):
  - Author Agent (has 'propose_test_cases'): returns that tool call
    with a realistic list of scenario strings -> real proposal rows
    get saved and appear in the UI.
  - Explorer Agent (has 'done'): returns 'done' immediately.

Switch on with:  LLM_PROVIDER_TYPE=simulation
"""

import logging
from typing import Optional

from llm.base import LLMProvider

logger = logging.getLogger(__name__)

_CANNED_SCENARIOS = [
    "Verify a user can log in with valid credentials and reaches the home screen",
    "Verify login is rejected with a clear error when the password is wrong",
    "Verify the main search returns matching records for a valid query",
    "Verify an empty-state message shows when a search has no matches",
    "Verify required fields are validated before a form can be submitted",
    "Verify a successful submission shows a confirmation message",
    "Verify results can be exported and the export contains the visible columns",
    "Verify an unauthorised user is denied access to a restricted record",
]


class _SimContent:
    """Stand-in for a Gemini content object; agents append this to history."""
    def __init__(self, tool_name, args):
        self.role = "model"
        self.parts = [{"function_call": {"name": tool_name, "args": args}}]


class _SimCandidate:
    def __init__(self, tool_name, args):
        self.content = _SimContent(tool_name, args)


class _SimResponse:
    """
    Mimics the shape agents rely on:
      response.candidates[0].content   (appended to conversation history)
    plus carries the tool name/args for extract_function_call().
    """
    def __init__(self, tool_name: Optional[str], args: dict):
        self.tool_name = tool_name
        self.args = args
        self.candidates = [_SimCandidate(tool_name, args)]


class SimulationProvider(LLMProvider):
    """No-LLM provider returning realistic canned output per agent."""

    def __init__(self, model: str = "sim-1.0"):
        self.model = model
        self._served = False
        self._tool_names = set()
        logger.info("SimulationProvider active — no real LLM calls will be made")

    def build_tool(self, tool_definitions: list[dict]) -> object:
        self._tool_names = {
            (t.get("name") if isinstance(t, dict) else getattr(t, "name", None))
            for t in (tool_definitions or [])
        }
        return tool_definitions

    def call_model(self, system_prompt, tool, contents, max_retries: Optional[int] = None,
                   options: Optional[dict] = None) -> object:
        # options is accepted and ignored — there is no model to configure.
        names = set(self._tool_names)
        if not names and tool:
            names = {
                (t.get("name") if isinstance(t, dict) else getattr(t, "name", None))
                for t in tool
            }

        if "propose_test_cases" in names:
            return _SimResponse("propose_test_cases", {"test_cases": _CANNED_SCENARIOS})

        return _SimResponse("done", {"passed": True, "notes": "Completed in simulation mode (no LLM)."})

    def extract_function_call(self, response: object) -> tuple[Optional[str], Optional[dict]]:
        resp: _SimResponse = response
        if self._served:
            return None, None
        self._served = True
        return resp.tool_name, resp.args

    def function_response_content(self, name: str, result: object) -> object:
        return {"role": "tool", "name": name, "content": str(result)}

    def user_text_content(self, text: str) -> object:
        return {"role": "user", "content": text}
