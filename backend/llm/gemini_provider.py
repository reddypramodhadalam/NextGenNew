"""
llm/gemini_provider.py

Gemini implementation of LLMProvider.

This is the only file that imports google.genai. Every agent
depends on llm.base.LLMProvider, never on this class directly.
Swapping to a different provider means writing a new class here —
nothing in agents/ changes.
"""

import logging
import re
import time
from typing import Optional

from google import genai
from google.genai import types, errors

from llm.base import LLMProvider, LLMProviderError
from core.db.queries import ai_calls as ai_calls_db

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    """
    Calls the Gemini API using manual (non-automatic) function calling.
    Handles two distinct failure modes differently:
    - ServerError (503): transient overload — short exponential backoff.
    - ClientError 429: rate limit — parse the API's own suggested delay
      and wait exactly that long, not a guessed value.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        max_retries: int = 3,
        timeout_per_step: int = 30,
        langfuse_url: Optional[str] = None,
        langfuse_api_key: Optional[str] = None,
    ):
        """
        Args:
            api_key: The Gemini API key from LLM_API_KEY env var.
            model: The model name, e.g. 'gemini-2.5-flash'.
            max_retries: Max retry attempts for transient failures.
            timeout_per_step: Per-step timeout in seconds.
            langfuse_url: Optional Langfuse endpoint for observability.
            langfuse_api_key: Optional Langfuse API key.
        """
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.max_retries = max_retries
        self.timeout_per_step = timeout_per_step
        self._langfuse_enabled = bool(langfuse_url and langfuse_api_key)
        if self._langfuse_enabled:
            logger.info("Langfuse observability enabled, url=%s", langfuse_url)

    def build_tool(self, tool_definitions: list[dict]) -> types.Tool:
        """See LLMProvider.build_tool docstring."""
        declarations = [
            types.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=t["input_schema"],
            )
            for t in tool_definitions
        ]
        return types.Tool(function_declarations=declarations)

    def call_model(
        self,
        system_prompt: str,
        tool: types.Tool,
        contents: list,
        max_retries: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> types.GenerateContentResponse:
        """
        See LLMProvider.call_model docstring.

        `options` carries Ollama-style generation settings (num_ctx and the
        like) which have no Gemini equivalent — the context window is fixed
        by the model. Accepted and ignored so callers stay provider-agnostic.
        """
        retries = max_retries if max_retries is not None else self.max_retries
        last_error = None

        for attempt in range(retries):
            start_ms = int(time.time() * 1000)
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=[tool],
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    ),
                )
                latency_ms = int(time.time() * 1000) - start_ms
                self._record_call(response, latency_ms)
                return response

            except errors.ServerError as e:
                last_error = e
                if attempt < retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Gemini server error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, retries, wait, e,
                    )
                    time.sleep(wait)

            except errors.ClientError as e:
                last_error = e
                if _is_rate_limit(e) and attempt < retries - 1:
                    wait = _parse_retry_delay(e) or 60
                    logger.warning(
                        "Gemini rate limit (attempt %d/%d), waiting %ds: %s",
                        attempt + 1, retries, wait, e,
                    )
                    time.sleep(wait)
                else:
                    raise LLMProviderError(str(e)) from e

        raise LLMProviderError(
            f"Gemini call failed after {retries} attempts: {last_error}"
        ) from last_error

    def extract_function_call(
        self, response: types.GenerateContentResponse
    ) -> tuple[Optional[str], Optional[dict]]:
        """See LLMProvider.extract_function_call docstring."""
        candidate = response.candidates[0]
        for part in candidate.content.parts:
            if part.function_call:
                return part.function_call.name, dict(part.function_call.args)
        return None, None

    def function_response_content(self, name: str, result: object) -> types.Content:
        """See LLMProvider.function_response_content docstring."""
        return types.Content(
            role="user",
            parts=[
                types.Part.from_function_response(
                    name=name, response={"result": result}
                )
            ],
        )

    def user_text_content(self, text: str) -> types.Content:
        """See LLMProvider.user_text_content docstring."""
        return types.Content(
            role="user",
            parts=[types.Part.from_text(text=text)],
        )

    def _record_call(
        self, response: types.GenerateContentResponse, latency_ms: int
    ) -> None:
        """
        Records token usage and latency to the ai_calls table.
        Called after every successful model response.
        """
        try:
            usage = response.usage_metadata
            if usage:
                ai_calls_db.record(
                    related_to_type="generation",
                    related_to_id=0,  # Caller should override via subclass or wrapper
                    provider="gemini",
                    model=self.model,
                    input_tokens=getattr(usage, "prompt_token_count", None),
                    output_tokens=getattr(usage, "candidates_token_count", None),
                    cost_usd=None,  # TODO: compute from token counts once pricing is confirmed
                    latency_ms=latency_ms,
                )
        except Exception:
            logger.exception("Failed to record AI call metrics — non-fatal")


def _is_rate_limit(error: errors.ClientError) -> bool:
    return getattr(error, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(error)


def _parse_retry_delay(error: errors.ClientError) -> Optional[int]:
    """
    Parses the server-suggested retry delay from a 429 error's details.
    Returns seconds as an int, or None if the structure isn't present.
    """
    try:
        for detail in error.details["error"]["details"]:
            if detail.get("@type", "").endswith("RetryInfo"):
                match = re.match(r"(\d+)", detail["retryDelay"])
                if match:
                    return int(match.group(1)) + 1  # +1s safety margin
    except (KeyError, TypeError, AttributeError):
        pass
    return None
