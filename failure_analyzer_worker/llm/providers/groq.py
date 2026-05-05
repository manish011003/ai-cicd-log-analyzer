"""Groq chat provider (wraps ``langchain_groq.ChatGroq``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

import httpx

if TYPE_CHECKING:
    from ...config import WorkerSettings
    from ..base import ChatMessage

logger = logging.getLogger(__name__)


def _tls_verify(raw: str) -> bool:
    return (raw or "").strip().lower() not in {"0", "false", "no", "off"}


class GroqLLM:
    """LLMClient backed by Groq's hosted inference."""

    def __init__(self, settings: "WorkerSettings") -> None:
        from langchain_groq import ChatGroq

        api_key = settings.llm_api_key or settings.groq_api_key
        if not api_key:
            raise ValueError(
                "LLM_API_KEY (or legacy GROQ_API_KEY) is required for LLM_PROVIDER=groq"
            )

        verify = _tls_verify(settings.llm_tls_verify)
        if not verify:
            logger.warning(
                "LLM_TLS_VERIFY is disabled — Groq TLS certificate will not be checked. "
                "Use this only behind a trusted MITM proxy.",
            )
        http_client = httpx.Client(verify=verify)
        self._model = ChatGroq(
            model=settings.llm_model,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
            groq_api_key=api_key,
            http_client=http_client,
        )

    def invoke(self, messages: Sequence["ChatMessage"]) -> str:
        from .._lc import extract_text, to_lc_messages

        resp = self._model.invoke(to_lc_messages(messages))
        return extract_text(resp.content)
