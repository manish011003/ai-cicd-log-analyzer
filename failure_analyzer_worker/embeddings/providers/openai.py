"""OpenAI-hosted embeddings (and OpenAI-compatible gateways via ``EMBEDDING_API_BASE``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ...config import WorkerSettings

logger = logging.getLogger(__name__)


# Known native dimensions for OpenAI-hosted embedding models. Callers can
# override via EMBEDDING_DIMENSIONS (OpenAI's 3-series supports shortening).
_NATIVE_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class OpenAIEmbedder:
    """Embedder backed by the OpenAI embeddings endpoint."""

    def __init__(self, settings: "WorkerSettings") -> None:
        from openai import OpenAI

        api_key = settings.embedding_api_key or settings.llm_api_key
        if not api_key:
            raise ValueError(
                "EMBEDDING_API_KEY (or LLM_API_KEY) is required for EMBEDDING_PROVIDER=openai",
            )

        kwargs: dict = {"api_key": api_key}
        if settings.embedding_api_base:
            kwargs["base_url"] = settings.embedding_api_base
        self._client = OpenAI(**kwargs)

        self._model = settings.embedding_model
        override = int(settings.embedding_dimensions or 0)
        self._dims = override or _NATIVE_DIMS.get(self._model, 1536)
        self._send_dimensions = bool(override) and override != _NATIVE_DIMS.get(self._model, override)

        logger.info(
            "OpenAI embedder  model=%s  dims=%d  shorten=%s",
            self._model,
            self._dims,
            self._send_dimensions,
        )

    @property
    def dimensions(self) -> int:
        return self._dims

    def _extra(self) -> dict:
        return {"dimensions": self._dims} if self._send_dimensions else {}

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(
            model=self._model, input=text, **self._extra(),
        )
        return list(resp.data[0].embedding)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(
            model=self._model, input=list(texts), **self._extra(),
        )
        return [list(item.embedding) for item in resp.data]
