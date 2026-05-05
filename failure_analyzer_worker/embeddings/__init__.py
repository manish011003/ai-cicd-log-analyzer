"""Pluggable embedding providers.

Callers depend only on :class:`Embedder`; the concrete library
(sentence-transformers, OpenAI, …) is selected at runtime by
:func:`create_embedder` based on ``EMBEDDING_PROVIDER``.
"""

from .base import Embedder
from .factory import create_embedder

__all__ = ["Embedder", "create_embedder"]
