"""
llm/ollama_provider.py

Ollama implementation of LLMProvider — runs a model LOCALLY (no data
leaves the machine, which is the compliance story for regulated
environments).

Enable with:
    LLM_PROVIDER_TYPE=ollama
    LLM_API_URL=http://localhost:11434
    LLM_MODEL=qwen2.5-coder:14b
    LLM_API_KEY=not-needed          (Ollama needs no key)

IMPORTANT — response shape:
The agents in agents/ were written against Gemini and do:
    contents.append(response.candidates[0].content)
BEFORE inspecting the tool call. So this provider wraps Ollama's reply
in a Gemini-compatible object exposing .candidates[0].content.parts[]
with .function_call.name / .function_call.args. Without that the Author
agent raises AttributeError and produces zero scenarios.
"""

import json
import logging
import time
from typing import Optional

import requests

from llm.base import LLMProvider, LLMProviderError

logger = logging.getLogger(__name__)


# ─────────────────────── Gemini-compatible wrappers ───────────────────────

class _FunctionCall:
    """Mirrors Gemini's part.function_call (has .name and .args)."""

    def __init__(self, name: str, args: dict):
        self.name = name
        self.args = args or {}


class _Part:
    """
    Mirrors a Gemini content part. Either carries a function_call or plain
    text; agents check `part.function_call` and ignore text parts.
    """

    def __init__(self, function_call: Optional[_FunctionCall] = None, text: str = ""):
        self.function_call = function_call
        self.text = text


class _Content:
    """
    Mirrors Gemini's Content. Agents append this to their history, so it
    must also be convertible back into an Ollama chat message — that's
    what to_message() is for.
    """

    def __init__(self, role: str, parts: list[_Part], raw_message: dict):
        self.role = role
        self.parts = parts
        self._raw = raw_message

    def to_message(self) -> dict:
        """Return the original Ollama message, for replaying history."""
        return self._raw


class _Candidate:
    def __init__(self, content: _Content):
        self.content = content


class _OllamaResponse:
    """Exposes .candidates[0].content exactly like Gemini's response."""

    def __init__(self, content: _Content, usage: dict, latency_ms: int = 0, model: str = ""):
        self.candidates = [_Candidate(content)]
        self.usage = usage
        self.latency_ms = latency_ms
        self.model = model


class _UserContent:
    """A plain user/tool message we created (not from the model)."""

    def __init__(self, message: dict):
        self._raw = message
        self.role = message.get("role", "user")
        self.parts = [_Part(text=message.get("content", ""))]

    def to_message(self) -> dict:
        return self._raw


# ───────────────────────────── provider ─────────────────────────────

class OllamaProvider(LLMProvider):
    """
    Calls a locally running Ollama server using its /api/chat endpoint
    with tool (function) calling.

    Nothing is sent to a third party — the model runs on this machine.
    """

    def __init__(
        self,
        api_url: str,
        model: str,
        max_retries: int = 3,
        timeout: int = 180,
    ):
        """
        Args:
            api_url: Ollama base URL, e.g. 'http://localhost:11434'.
            model: Model tag, e.g. 'qwen2.5-coder:14b'.
            max_retries: Retries for transient connection failures.
            timeout: Per-request timeout in seconds (local models can be
                slower than a hosted API, so this is generous).
        """
        self.base_url = api_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout
        logger.info("OllamaProvider active — model=%s url=%s (local, no data leaves this machine)",
                    model, self.base_url)

    # ---- interface ----

    def build_tool(self, tool_definitions: list[dict]) -> list[dict]:
        """
        Convert internal tool definitions to Ollama's (OpenAI-style)
        function schema.

        Args:
            tool_definitions: dicts with 'name', 'description', 'input_schema'.

        Returns:
            List of Ollama tool dicts, passed straight back into call_model.
        """
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
    ) -> _OllamaResponse:
        """
        Send the conversation to Ollama and return a Gemini-shaped response.

        Args:
            system_prompt: The agent's system prompt.
            tool: Tool list from build_tool().
            contents: Conversation history (objects from this provider).
            max_retries: Override the configured retry count.
            options: Per-call overrides merged over the defaults below —
                num_ctx and num_predict are the ones that matter. The
                defaults are sized for a 16GB card; raising num_ctx doubles
                the KV cache and can spill the model to CPU, so it is opt-in
                per call rather than a global change.

        Returns:
            _OllamaResponse exposing .candidates[0].content.

        Raises:
            LLMProviderError: if the server is unreachable or returns an
                error after all retries.
        """
        retries = self.max_retries if max_retries is None else max_retries

        messages = [{"role": "system", "content": system_prompt}]
        for item in contents:
            if hasattr(item, "to_message"):
                messages.append(item.to_message())
            elif isinstance(item, dict):
                messages.append(item)
            else:  # defensive: stringify anything unexpected
                messages.append({"role": "user", "content": str(item)})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            # Keep the model resident in VRAM. Ollama unloads after ~5 idle
            # minutes by default, which would cause a slow cold reload in the
            # middle of a demo.
            "keep_alive": "60m",
            "options": {
                "temperature": 0.1,   # deterministic-ish for code/tools
                # NOTE: the two values below are DEFAULTS. A caller can raise
                # them per call via the `options` argument — see the merge
                # below. Nothing does by default; the survey path opts in.
                # Ollama defaults to 2048 tokens, which silently truncates
                # our prompts and yields an empty reply. 4096 is enough for
                # the trimmed data snapshot while keeping the KV cache small
                # enough to stay in VRAM on a 16GB GPU (a 14B model + a large
                # context can otherwise spill to CPU and become very slow).
                "num_ctx": 4096,
                # A cap, not a target: the model stops when it's done, so a high
                # value doesn't slow short chat replies. It must be large enough
                # for a full Selenium script — at 300 the script was truncated
                # mid-string, producing "unterminated string literal".
                "num_predict": 1500,
            },
        }
        # Per-call overrides win over the defaults above. Merged rather than
        # replaced so a caller that only wants a bigger num_ctx does not
        # silently lose temperature.
        if options:
            payload["options"].update(options)
            logger.info("Ollama per-call options: %s", options)
        if tool:
            payload["tools"] = tool

        last_err = None
        for attempt in range(retries):
            try:
                started = time.time()
                logger.info("Calling Ollama (model=%s, %s messages) — first call loads the model into VRAM and can take ~30-60s",
                            self.model, len(messages))
                r = requests.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                    timeout=self.timeout,
                )
                r.raise_for_status()
                data = r.json()
                latency_ms = int((time.time() - started) * 1000)
                logger.info("Ollama call ok in %sms (model=%s)", latency_ms, self.model)
                return self._wrap(data, latency_ms)

            except requests.exceptions.ConnectionError as e:
                last_err = e
                logger.warning(
                    "Cannot reach Ollama at %s (attempt %s/%s). Is `ollama serve` running?",
                    self.base_url, attempt + 1, retries,
                )
                time.sleep(2 ** attempt)
            except requests.exceptions.Timeout as e:
                last_err = e
                logger.warning("Ollama timed out after %ss (attempt %s/%s)",
                               self.timeout, attempt + 1, retries)
                time.sleep(2 ** attempt)
            except requests.exceptions.HTTPError as e:
                # A 404 here usually means the model tag isn't pulled yet.
                body = getattr(e.response, "text", "")
                if getattr(e.response, "status_code", None) == 404:
                    raise LLMProviderError(
                        f"Ollama has no model '{self.model}'. Run:  ollama pull {self.model}"
                    ) from e
                last_err = e
                logger.warning("Ollama HTTP error (attempt %s/%s): %s",
                               attempt + 1, retries, body[:300])
                time.sleep(2 ** attempt)

        raise LLMProviderError(
            f"Ollama call failed after {retries} attempts at {self.base_url}. "
            f"Check that Ollama is running and '{self.model}' is pulled. Last error: {last_err}"
        )

    def extract_function_call(self, response: _OllamaResponse) -> tuple[Optional[str], Optional[dict]]:
        """Return (tool_name, args) from the model's reply, or (None, None)."""
        candidate = response.candidates[0]
        for part in candidate.content.parts:
            if part.function_call:
                return part.function_call.name, dict(part.function_call.args)
        return None, None

    def function_response_content(self, name: str, result: object) -> _UserContent:
        """Wrap a tool result as a message to send back to the model."""
        return _UserContent({
            "role": "tool",
            "content": json.dumps({"result": result}, default=str)[:12000],
        })

    def user_text_content(self, text: str) -> _UserContent:
        """Wrap plain user text as a message."""
        return _UserContent({"role": "user", "content": text})

    # ---- internals ----

    def _wrap(self, data: dict, latency_ms: int = 0) -> _OllamaResponse:
        """
        Convert Ollama's reply into the Gemini-compatible shape the agents
        expect.

        Ollama returns:
            {"message": {"role": "assistant", "content": "...",
                         "tool_calls": [{"function": {"name": .., "arguments": {..}}}]}}
        """
        msg = data.get("message", {}) or {}
        parts: list[_Part] = []

        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {}) or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):  # some builds return a JSON string
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    logger.warning("Could not parse tool arguments as JSON: %s", args[:200])
                    args = {}
            parts.append(_Part(function_call=_FunctionCall(fn.get("name", ""), args)))

        text = msg.get("content") or ""
        if text:
            parts.append(_Part(text=text))
        if not parts:
            parts.append(_Part(text=""))

        content = _Content(role=msg.get("role", "assistant"), parts=parts, raw_message=msg)
        usage = {
            "input_tokens": data.get("prompt_eval_count"),
            "output_tokens": data.get("eval_count"),
        }
        return _OllamaResponse(content, usage, latency_ms=latency_ms, model=self.model)
