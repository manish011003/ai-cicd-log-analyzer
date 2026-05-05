"""Factory that turns ``LLM_PROVIDER`` into a concrete :class:`LLMClient`.

Providers are imported lazily so you do not need every vendor's SDK
installed — only the one you select actually has to be importable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import WorkerSettings
    from .base import LLMClient

logger = logging.getLogger(__name__)


def create_llm(settings: "WorkerSettings") -> "LLMClient":
    """Return the LLM client that matches ``settings.llm_provider``.

    Raises:
        ValueError: on an unknown provider or when required settings are missing.
    """
    provider = (settings.llm_provider or "").strip().lower()
    logger.info("Creating LLM client  provider=%s  model=%s", provider, settings.llm_model)

    if provider == "groq":
        from .providers.groq import GroqLLM

        return GroqLLM(settings)
    if provider == "openai":
        from .providers.openai import OpenAILLM

        return OpenAILLM(settings)
    if provider == "anthropic":
        from .providers.anthropic import AnthropicLLM

        return AnthropicLLM(settings)
    if provider == "ollama":
        from .providers.ollama import OllamaLLM

        return OllamaLLM(settings)

    raise ValueError(
        f"Unknown LLM_PROVIDER={provider!r}. "
        "Supported: groq, openai, anthropic, ollama."
    )
