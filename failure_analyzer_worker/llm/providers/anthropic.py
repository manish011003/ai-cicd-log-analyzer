"""Anthropic Claude chat provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ...config import WorkerSettings
    from ..base import ChatMessage


class AnthropicLLM:
    """LLMClient backed by ``langchain_anthropic.ChatAnthropic``."""

    def __init__(self, settings: "WorkerSettings") -> None:
        from langchain_anthropic import ChatAnthropic

        if not settings.llm_api_key:
            raise ValueError("LLM_API_KEY is required for LLM_PROVIDER=anthropic")

        kwargs: dict = {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
            "max_tokens": settings.llm_max_tokens or 2048,
            "api_key": settings.llm_api_key,
        }
        if settings.llm_api_base:
            kwargs["base_url"] = settings.llm_api_base

        self._model = ChatAnthropic(**kwargs)

    def invoke(self, messages: Sequence["ChatMessage"]) -> str:
        from .._lc import extract_text, to_lc_messages

        resp = self._model.invoke(to_lc_messages(messages))
        return extract_text(resp.content)
