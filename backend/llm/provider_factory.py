"""
llm/provider_factory.py

Reads LLM configuration from settings and instantiates the correct
provider implementation. Every agent calls get_provider() — none of
them import a specific provider class directly.
"""

import logging
from llm.base import LLMProvider
from core.settings import settings

logger = logging.getLogger(__name__)


def get_provider() -> LLMProvider:
    """
    Returns the configured LLM provider instance.

    The provider is chosen by settings.llm.provider_type (the
    LLM_PROVIDER_TYPE environment variable, already defined in
    llm.yaml). This replaces the previous approach of guessing the
    provider from the API URL, which broke for Azure OpenAI: its URL
    is '<resource>.openai.azure.com', which contains 'openai' but not
    'openai.com', so it matched none of the URL checks and always
    raised. Reading an explicit field fixes that permanently.

    Supported: ollama | simulation | gemini | azure_openai | bedrock
    (openai remains not-yet-implemented in this repository.)

    Returns:
        An LLMProvider instance ready to call.

    Raises:
        ValueError: If provider_type is not a recognised value.
        NotImplementedError: If it's recognised but has no implementation yet.
    """
    provider_type = settings.llm.provider_type.lower().strip()

    if provider_type == "ollama":
        from llm.ollama_provider import OllamaProvider
        return OllamaProvider(
            api_url=settings.llm.api_url,
            model=settings.llm.model,
            max_retries=settings.llm.max_retries,
        )

    if provider_type == "simulation":
        logger.info("Using SIMULATION provider — no real LLM calls")
        from llm.simulation_provider import SimulationProvider
        return SimulationProvider(model=settings.llm.model or "sim-1.0")

    if provider_type == "gemini":
        logger.info("Using Gemini LLM provider, model=%s", settings.llm.model)
        from llm.gemini_provider import GeminiProvider
        return GeminiProvider(
            api_key=settings.llm.api_key,
            model=settings.llm.model,
            max_retries=settings.llm.max_retries,
            timeout_per_step=settings.llm.timeout_per_step,
            langfuse_url=settings.llm.langfuse_url,
            langfuse_api_key=settings.llm.langfuse_api_key,
        )

    if provider_type == "azure_openai":
        logger.info("Using Azure OpenAI LLM provider, deployment=%s", settings.llm.azure_deployment_name)
        from llm.azure_openai_provider import AzureOpenAIProvider
        return AzureOpenAIProvider(
            api_url=settings.llm.api_url,
            api_key=settings.llm.api_key,
            api_version=settings.llm.azure_api_version,
            deployment_name=settings.llm.azure_deployment_name,
            max_retries=settings.llm.max_retries,
            timeout_per_step=settings.llm.timeout_per_step,
        )

    if provider_type == "bedrock":
        logger.info("Using Bedrock LLM provider via internal gateway, model=%s", settings.llm.model)
        from llm.bedrock_provider import BedrockProvider
        return BedrockProvider(
            api_url=settings.llm.api_url,
            api_key=settings.llm.api_key,
            model=settings.llm.model,
            max_retries=settings.llm.max_retries,
            timeout_per_step=settings.llm.timeout_per_step,
        )

    if provider_type == "openai":
        raise NotImplementedError(
            "Standard (non-Azure) OpenAI provider is not yet implemented. "
            "See llm/base.py for the interface to implement."
        )

    raise ValueError(
        f"Unknown LLM_PROVIDER_TYPE='{settings.llm.provider_type}'. "
        f"Supported values: ollama, simulation, gemini, azure_openai, bedrock."
    )
