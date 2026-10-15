"""
llm/bedrock_provider.py

Internal AWS Bedrock gateway implementation of LLMProvider.

This provider targets an OpenAI-compatible chat endpoint exposed by an
internal gateway (base URL from LLM_API_URL), while keeping the provider
name and wiring explicit as `bedrock`.

Important compatibility detail:
Agents append `response.candidates[0].content` directly to conversation
history. This provider therefore wraps gateway responses in a Gemini-like
shape while preserving raw assistant/tool-call messages for replay.

Every successful call records its token usage and cost to ai_calls
(see _record_call).
"""

import json
import logging
import time
from typing import Optional

import requests

from llm.base import LLMProvider, LLMProviderError
from core.db.queries import ai_calls as ai_calls_db

logger = logging.getLogger(__name__)

# Provider name saved in ai_calls. It must match the provider column in llm_model_pricing.
PROVIDER_NAME = "bedrock"


class _FunctionCall:
    """Mirrors Gemini's part.function_call with .name and .args."""

    def __init__(self, name: str, args: dict):
        self.name = name
        self.args = args or {}


class _Part:
    """Carries either a tool call or plain text."""

    def __init__(self, function_call: Optional[_FunctionCall] = None, text: str = ""):
        self.function_call = function_call
        self.text = text


class _Content:
    """
    Gemini-like content container the agents can append to history.
    The raw gateway message is kept so the provider can replay history.
    """

    def __init__(self, role: str, parts: list[_Part], raw_message: dict):
        self.role = role
        self.parts = parts
        self._raw = raw_message

    def to_message(self) -> dict:
        return self._raw


class _Candidate:
    def __init__(self, content: _Content):
        self.content = content


class _GatewayResponse:
    """Exposes response.candidates[0].content for existing agents."""

    def __init__(self, content: _Content, usage: dict, model: str):
        self.candidates = [_Candidate(content)]
        self.usage = usage
        self.model = model


class _UserContent:
    """Message wrapper for user/tool turns we create."""

    def __init__(self, message: dict):
        self._raw = message
        self.role = message.get("role", "user")
        self.parts = [_Part(text=message.get("content", ""))]

    def to_message(self) -> dict:
        return self._raw


class BedrockProvider(LLMProvider):
    """Provider for internal Bedrock gateway endpoints."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str,
        max_retries: int = 3,
        timeout_per_step: int = 30,
    ):
        self.base_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout_per_step
        self._last_tool_call_id: Optional[str] = None

        logger.info(
            "BedrockProvider active via internal gateway (url=%s, model=%s)",
            self.base_url,
            self.model,
        )

    def build_tool(self, tool_definitions: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in (tool_definitions or [])
        ]

    def call_model(
        self,
        system_prompt: str,
        tool: list[dict],
        contents: list,
        max_retries: Optional[int] = None,
        options: Optional[dict] = None,
    ) -> _GatewayResponse:
        retries = self.max_retries if max_retries is None else max_retries

        messages = [{"role": "system", "content": system_prompt}]
        for item in contents:
            if hasattr(item, "to_message"):
                messages.append(item.to_message())
            elif isinstance(item, dict):
                messages.append(item)
            else:
                messages.append({"role": "user", "content": str(item)})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
        }
        # Only include tool config when there are actual tools.
        # Sending tool_choice="auto" with an empty tools list causes the
        # Bedrock gateway to return an error, which manifests as 0 scenarios
        # in fast_survey (all chunks silently fail).
        if tool:
            payload["tools"] = tool
            payload["tool_choice"] = "auto"
        # Note: Ollama-specific options (e.g. num_predict) are not forwarded
        # to the Bedrock gateway — the gateway does not accept unknown fields
        # and returns 400 Bad Request if they are present.

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_err = None
        for attempt in range(retries):
            try:
                started = time.time()
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=self.timeout if self.timeout > 0 else None,
                )

                if r.status_code == 429:
                    wait = _parse_retry_after(r.headers.get("Retry-After")) or 60
                    if attempt < retries - 1:
                        logger.warning(
                            "Bedrock gateway rate limit (attempt %s/%s), waiting %ss",
                            attempt + 1,
                            retries,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    raise LLMProviderError(f"Rate limited by Bedrock gateway: {r.text[:300]}")

                if 500 <= r.status_code <= 599:
                    if attempt < retries - 1:
                        wait = 2 ** (attempt + 1)
                        logger.warning(
                            "Bedrock gateway server error %s (attempt %s/%s), retrying in %ss",
                            r.status_code,
                            attempt + 1,
                            retries,
                            wait,
                        )
                        time.sleep(wait)
                        continue
                    raise LLMProviderError(
                        f"Bedrock gateway server error {r.status_code}: {r.text[:300]}"
                    )

                r.raise_for_status()
                data = r.json()
                latency_ms = int((time.time() - started) * 1000)

                # Build the response first, then save usage. Saving never raises,
                # so the caller always gets its response.
                response = self._wrap(data)
                self._record_call(response, latency_ms)
                return response

            except requests.exceptions.Timeout as e:
                last_err = e
                if attempt < retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Bedrock gateway timeout (attempt %s/%s), retrying in %ss",
                        attempt + 1,
                        retries,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                raise LLMProviderError(f"Bedrock gateway timeout after {retries} attempts") from e

            except requests.exceptions.RequestException as e:
                last_err = e
                if attempt < retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Bedrock gateway request error (attempt %s/%s), retrying in %ss: %s",
                        attempt + 1,
                        retries,
                        wait,
                        e,
                    )
                    time.sleep(wait)
                    continue
                raise LLMProviderError(f"Bedrock gateway request failed: {e}") from e

        raise LLMProviderError(
            f"Bedrock gateway call failed after {retries} attempts. Last error: {last_err}"
        )

    def extract_function_call(self, response: _GatewayResponse) -> tuple[Optional[str], Optional[dict]]:
        candidate = response.candidates[0]
        for part in candidate.content.parts:
            if part.function_call:
                return part.function_call.name, dict(part.function_call.args)
        return None, None

    def function_response_content(self, name: str, result: object) -> _UserContent:
        return _UserContent(
            {
                "role": "tool",
                "tool_call_id": self._last_tool_call_id,
                "content": json.dumps({"result": result}, default=str),
            }
        )

    def user_text_content(self, text: str) -> _UserContent:
        return _UserContent({"role": "user", "content": text})

    def _record_call(self, response, latency_ms):
        """
        Saves token usage, cost, and latency for one successful call.
        Uses the configured model name, so the price lookup matches llm_model_pricing.
        Never raises: a bad usage block must not fail a call that succeeded.
        """
        # Only a dictionary usage block can be read; anything else is treated as missing.
        usage = response.usage if isinstance(response.usage, dict) else {}
        if not usage:
            # Still saved (with empty token counts) so the call is visible and can be investigated.
            logger.warning("Bedrock gateway response had no usable usage block; tokens recorded as NULL")

        ai_calls_db.record_llm_call(
            provider=PROVIDER_NAME,
            model=self.model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=latency_ms,
        )


    def _wrap(self, data: dict) -> _GatewayResponse:
        choices = data.get("choices") or []
        if not choices:
            content = _Content(role="assistant", parts=[_Part(text="")], raw_message={"role": "assistant", "content": ""})
            # Keeps usage even when there are no choices, because those tokens were still billed.
            return _GatewayResponse(content, usage=data.get("usage") or {}, model=self.model)

        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []

        parts: list[_Part] = []
        for tc in tool_calls:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    logger.warning("Could not parse tool arguments as JSON: %s", args[:200])
                    args = {}

            self._last_tool_call_id = (tc or {}).get("id")
            parts.append(_Part(function_call=_FunctionCall(fn.get("name", ""), args)))

        text = message.get("content") or ""
        if text:
            parts.append(_Part(text=text))
        if not parts:
            parts.append(_Part(text=""))

        assistant_raw = {
            "role": "assistant",
            "content": text,
        }
        if tool_calls:
            assistant_raw["tool_calls"] = tool_calls

        content = _Content(
            role=message.get("role", "assistant"),
            parts=parts,
            raw_message=assistant_raw,
        )
        usage = data.get("usage") or {}
        return _GatewayResponse(content, usage=usage, model=data.get("model", self.model))


def _parse_retry_after(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError):
        return None
