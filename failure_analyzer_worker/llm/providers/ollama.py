"""Ollama chat provider for fully local / air-gapped deployments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ...config import WorkerSettings
    from ..base import ChatMessage


class OllamaLLM:
    """LLMClient backed by ``langchain_ollama.ChatOllama``."""

    def __init__(self, settings: "WorkerSettings") -> None:
        from langchain_ollama import ChatOllama

        kwargs: dict = {
            "model": settings.llm_model,
            "temperature": settings.llm_temperature,
        }
        if settings.llm_api_base:
            kwargs["base_url"] = settings.llm_api_base
        if settings.llm_max_tokens:
            kwargs["num_predict"] = settings.llm_max_tokens

        self._model = ChatOllama(**kwargs)

    def invoke(self, messages: Sequence["ChatMessage"]) -> str:
        from .._lc import extract_text, to_lc_messages

        resp = self._model.invoke(to_lc_messages(messages))
        return extract_text(resp.content)
