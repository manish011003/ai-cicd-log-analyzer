"""OpenAI chat provider.

Works with the OpenAI platform and any OpenAI-compatible endpoint (including
Azure OpenAI's OpenAI-compatible routes and self-hosted gateways) when
``LLM_API_BASE`` is set.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ...config import WorkerSettings
    from ..base import ChatMessage

logger = logging.getLogger(__name__)


class OpenAILLM:
    """LLMClient backed by ``langchain_openai.ChatOpenAI``."""

    def __init__(self, settings: "WorkerSettings") -> None:
        from langchain_openai import ChatOpenAI

        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required for LLM_PROVIDER=openai")

        kwargs: dict = {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "api_key": settings.llm_api_key,
        }
        if settings.llm_max_tokens:
            kwargs["max_tokens"] = settings.llm_max_tokens
        if settings.llm_api_base:
            kwargs["base_url"] = settings.llm_api_base

        self._model = ChatOpenAI(**kwargs)

    def invoke(self, messages: Sequence["ChatMessage"]) -> str:
        from .._lc import extract_text, to_lc_messages

        resp = self._model.invoke(to_lc_messages(messages))
        return extract_text(resp.content)
