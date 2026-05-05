"""Embedder protocol: produce dense vectors for arbitrary text.

The vector store uses :pyattr:`~Embedder.dimensions` to size its index at
creation time, so any implementation MUST report a stable dimension before
the first ``embed()`` call (typically derived from the loaded model).
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    """Contract expected by the vector store and retrieval layer."""

    @property
    def dimensions(self) -> int:  # pragma: no cover - protocol
        ...

    def embed(self, text: str) -> list[float]:  # pragma: no cover - protocol
        ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:  # pragma: no cover - protocol
        ...
