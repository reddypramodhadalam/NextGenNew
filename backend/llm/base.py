"""
llm/base.py

Abstract contract every LLM backend must implement.

Every agent depends only on this interface — never on a specific SDK.
Adding a new provider (AWS Bedrock, OpenAI, etc.) means writing one new
class that implements these five methods. Nothing in agents/ changes.
"""

from abc import ABC, abstractmethod
from typing import Optional


class LLMProvider(ABC):
    """
    Abstract base class for all LLM provider implementations.

    Implementations must be stateless with respect to individual requests —
    conversation history is maintained by the caller (agent loop), not here.
    """

    @abstractmethod
    def build_tool(self, tool_definitions: list[dict]) -> object:
        """
        Converts this codebase's own tool definition format into whatever
        object this provider's SDK expects.

        Args:
            tool_definitions: List of dicts, each with 'name', 'description',
                and 'input_schema' (a JSON Schema dict).

        Returns:
            A provider-specific tool object. Opaque to callers — only ever
            passed back into call_model().
        """
        raise NotImplementedError

    @abstractmethod
    def call_model(
        self,
        system_prompt: str,
        tool: object,
        contents: list,
        max_retries: int = 3,
        options: Optional[dict] = None,
    ) -> object:
        """
        Calls the model with the given conversation history.

        Must handle transient failures internally:
        - Server errors (503): short exponential backoff.
        - Rate limits (429): parse and honour the API's suggested delay.

        Args:
            system_prompt: The agent's system prompt.
            tool: The tool object returned by build_tool().
            contents: The full conversation history in this provider's format.
            max_retries: How many attempts before giving up.
            options: Optional per-call generation settings, merged over the
                provider's defaults. Only some providers act on it — a
                provider that has no equivalent must accept and ignore it
                rather than raise, so a caller never has to know which
                backend it is talking to. Keys are provider-specific; the
                survey path uses {"num_ctx", "num_predict"} for Ollama.

        Returns:
            A provider-specific response object, passed straight into
            extract_function_call(). Opaque to callers.

        Raises:
            LLMProviderError: After all retries are exhausted.
        """
        raise NotImplementedError

    @abstractmethod
    def extract_function_call(self, response: object) -> tuple[Optional[str], Optional[dict]]:
        """
        Extracts a tool call from a provider response.

        Args:
            response: The response object returned by call_model().

        Returns:
            (name, args) if the model made a tool call, or (None, None)
            if it responded with plain text instead.
        """
        raise NotImplementedError

    @abstractmethod
    def function_response_content(self, name: str, result: object) -> object:
        """
        Builds the next conversation turn reporting a tool's result.

        Args:
            name: The tool name that was called.
            result: The tool's return value (plain Python object).

        Returns:
            A provider-specific content object to append to the conversation.
        """
        raise NotImplementedError

    @abstractmethod
    def user_text_content(self, text: str) -> object:
        """
        Wraps a plain text string as a user turn in the conversation.

        Args:
            text: The message text.

        Returns:
            A provider-specific content object.
        """
        raise NotImplementedError


class LLMProviderError(Exception):
    """
    Raised when a provider call fails after all retries are exhausted.
    Callers catch this and record it as an 'incomplete' generation.
    """
    pass
