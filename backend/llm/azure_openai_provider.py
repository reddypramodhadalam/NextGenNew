"""
llm/azure_openai_provider.py

Azure OpenAI implementation of LLMProvider. This is the file that was
completely missing - not broken, absent - which is the real reason
switching to Azure OpenAI never worked, regardless of what key or URL
was configured. There was no code to actually call it.

This is the only file that imports the `openai` SDK's Azure client.
Every agent depends on llm.base.LLMProvider, never on this class
directly - nothing in agents/ needs to change for this to work.
"""

import json
import logging
import time
from typing import Optional

from openai import AzureOpenAI, APIStatusError, APIConnectionError, RateLimitError

from llm.base import LLMProvider, LLMProviderError
from core.db.queries import ai_calls as ai_calls_db

logger = logging.getLogger(__name__)


class AzureOpenAIProvider(LLMProvider):
    """
    Calls an Azure OpenAI deployment using standard tool calling.

    Handles two distinct failure modes differently, same split as the
    Gemini provider:
    - 5xx server errors: transient overload - short exponential backoff.
    - 429 rate limit: honours the API's own Retry-After header rather
      than a guessed wait time.

    One real difference from Gemini worth knowing: OpenAI's tool-call
    protocol requires echoing back the specific tool_call_id a result
    belongs to, not just the tool's name. The base interface's
    function_response_content() only takes a name and a result, so
    this class remembers the most recent tool_call_id internally,
    set during extract_function_call() and consumed by
    function_response_content() right after. This only works because
    the agent loop always calls those two methods in that order for a
    given turn - if that ever changes, this will need a real
    conversation-state object instead of one remembered id.
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        api_version: Optional[str],
        deployment_name: Optional[str],
        max_retries: int = 3,
        timeout_per_step: int = 30,
    ):
        """
        Args:
            api_url (str): the Azure resource endpoint, e.g.
                "https://baxter-openai.openai.azure.com/".
            api_key (str): the Azure OpenAI API key from LLM_API_KEY.
            api_version (str): Azure's API version string, e.g.
                "2024-08-01-preview", from AZURE_OPENAI_API_VERSION.
            deployment_name (str): the deployment name configured in
                Azure - NOT the underlying model name - from
                AZURE_OPENAI_DEPLOYMENT_NAME.
            max_retries (int): max retry attempts for transient failures.
            timeout_per_step (int): per-call timeout in seconds.

        Raises:
            RuntimeError: if api_version or deployment_name is missing -
                both are required for Azure specifically and have no
                sensible default.
        """
        if not api_version:
            raise RuntimeError(
                "AZURE_OPENAI_API_VERSION is required when "
                "LLM_PROVIDER_TYPE=azure_openai but was not set."
            )
        if not deployment_name:
            raise RuntimeError(
                "AZURE_OPENAI_DEPLOYMENT_NAME is required when "
                "LLM_PROVIDER_TYPE=azure_openai but was not set."
            )

        self.client = AzureOpenAI(
            azure_endpoint=api_url,
            api_key=api_key,
            api_version=api_version,
            timeout=timeout_per_step,
        )
        self.deployment_name = deployment_name
        self.max_retries = max_retries
        self.timeout_per_step = timeout_per_step
        self._last_tool_call_id: Optional[str] = None

    def build_tool(self, tool_definitions: list[dict]) -> list[dict]:
        """See LLMProvider.build_tool docstring."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tool_definitions
        ]

    def call_model(
        self,
        system_prompt: str,
        tool: list[dict],
        contents: list,
        max_retries: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> object:
        """
        See LLMProvider.call_model docstring.

        `options` carries Ollama-style generation settings (num_ctx and the
        like) with no Azure equivalent — the context window is a property of
        the deployment. Accepted and ignored so callers stay provider-agnostic.
        """
        retries = max_retries if max_retries is not None else self.max_retries
        messages = [{"role": "system", "content": system_prompt}] + contents
        last_error = None

        for attempt in range(retries):
            start_ms = int(time.time() * 1000)
            try:
                response = self.client.chat.completions.create(
                    model=self.deployment_name,
                    messages=messages,
                    tools=tool,
                    tool_choice="auto",
                )
                latency_ms = int(time.time() * 1000) - start_ms
                self._record_call(response, latency_ms)
                return response

            except RateLimitError as e:
                last_error = e
                if attempt < retries - 1:
                    wait = _parse_retry_after(e) or 60
                    logger.warning(
                        "Azure OpenAI rate limit (attempt %d/%d), waiting %ds: %s",
                        attempt + 1, retries, wait, e,
                    )
                    time.sleep(wait)
                else:
                    raise LLMProviderError(str(e)) from e

            except (APIStatusError, APIConnectionError) as e:
                last_error = e
                if attempt < retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Azure OpenAI server/connection error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, retries, wait, e,
                    )
                    time.sleep(wait)

        raise LLMProviderError(
            f"Azure OpenAI call failed after {retries} attempts: {last_error}"
        ) from last_error

    def extract_function_call(self, response: object) -> tuple[Optional[str], Optional[dict]]:
        """See LLMProvider.extract_function_call docstring."""
        message = response.choices[0].message
        if not message.tool_calls:
            return None, None

        tool_call = message.tool_calls[0]
        self._last_tool_call_id = tool_call.id
        return tool_call.function.name, json.loads(tool_call.function.arguments)

    def function_response_content(self, name: str, result: object) -> dict:
        """See LLMProvider.function_response_content docstring."""
        return {
            "role": "tool",
            "tool_call_id": self._last_tool_call_id,
            "content": json.dumps({"result": result}),
        }

    def user_text_content(self, text: str) -> dict:
        """See LLMProvider.user_text_content docstring."""
        return {"role": "user", "content": text}

    def _record_call(self, response: object, latency_ms: int) -> None:
        """
        Saves token usage, cost, and latency for one successful call.
        The activity label (test case or script generation) comes from the usage context.
        """
        usage = getattr(response, "usage", None)
        ai_calls_db.record_llm_call(
            provider="azure_openai",
            model=self.deployment_name,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            latency_ms=latency_ms,
        )


def _parse_retry_after(error: RateLimitError) -> Optional[int]:
    """
    Reads the Retry-After header Azure returns on a 429, if present.

    Args:
        error (RateLimitError): the raised exception.

    Returns:
        Optional[int]: seconds to wait, or None if the header isn't present.
    """
    try:
        header_value = error.response.headers.get("Retry-After")
        return int(header_value) if header_value else None
    except (AttributeError, ValueError, TypeError):
        return None
