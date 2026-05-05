"""Local CPU/GPU embeddings via ``sentence-transformers`` (default provider)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ...config import WorkerSettings

logger = logging.getLogger(__name__)


class SentenceTransformersEmbedder:
    """Embedder implementation that loads a HuggingFace sentence-transformer."""

    def __init__(self, settings: "WorkerSettings") -> None:
        from sentence_transformers import SentenceTransformer  # heavy: torch

        logger.info("Loading sentence-transformer model: %s", settings.embedding_model)
        self._model = SentenceTransformer(settings.embedding_model)

        model_dims = int(self._model.get_sentence_embedding_dimension() or 0)
        override = int(settings.embedding_dimensions or 0)
        if override and override != model_dims and model_dims:
            logger.warning(
                "EMBEDDING_DIMENSIONS=%d overrides model-reported dims (%d); "
                "ES mapping will follow the override.",
                override,
                model_dims,
            )
        self._dims = override or model_dims
        if not self._dims:
            raise RuntimeError(
                "Could not determine embedding dimensions — set EMBEDDING_DIMENSIONS.",
            )

    @property
    def dimensions(self) -> int:
        return self._dims

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return self._model.encode(list(texts), normalize_embeddings=True).tolist()
