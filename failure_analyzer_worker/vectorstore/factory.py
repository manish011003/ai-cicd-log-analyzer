"""Factory for :class:`SolutionRepository` backends."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import WorkerSettings
    from ..embeddings.base import Embedder
    from .base import SolutionRepository

logger = logging.getLogger(__name__)


def create_solution_repository(
    settings: "WorkerSettings",
    embedder: "Embedder",
) -> "SolutionRepository":
    """Build the solution repo that matches ``settings.vector_store_provider``."""
    provider = (settings.vector_store_provider or "").strip().lower()
    logger.info(
        "Creating solution repo  provider=%s  index=%s  dims=%d",
        provider,
        settings.elasticsearch_index,
        embedder.dimensions,
    )

    if provider == "elasticsearch":
        from .providers.elasticsearch import ElasticsearchSolutionRepository

        return ElasticsearchSolutionRepository(settings, embedder)

    raise ValueError(
        f"Unknown VECTOR_STORE_PROVIDER={provider!r}. Supported: elasticsearch.",
    )
