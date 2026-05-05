"""Factory that turns ``EMBEDDING_PROVIDER`` into a concrete :class:`Embedder`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import WorkerSettings
    from .base import Embedder

logger = logging.getLogger(__name__)


def create_embedder(settings: "WorkerSettings") -> "Embedder":
    """Return the embedder that matches ``settings.embedding_provider``."""
    provider = (settings.embedding_provider or "").strip().lower()
    logger.info(
        "Creating embedder  provider=%s  model=%s",
        provider,
        settings.embedding_model,
    )

    if provider in {"sentence_transformers", "sentence-transformers", "st"}:
        from .providers.sentence_transformers import SentenceTransformersEmbedder

        return SentenceTransformersEmbedder(settings)
    if provider == "openai":
        from .providers.openai import OpenAIEmbedder

        return OpenAIEmbedder(settings)

    raise ValueError(
        f"Unknown EMBEDDING_PROVIDER={provider!r}. "
        "Supported: sentence_transformers, openai."
    )
