"""Local CPU/GPU embeddings via ``sentence-transformers`` (default provider)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from ...config import WorkerSettings

logger = logging.getLogger(__name__)

_TLS_VERIFICATION_PATCHED = False


def _disable_requests_tls_verification() -> None:
    """Make HuggingFace downloads tolerate a corporate MITM proxy.

    Two things must happen for ``sentence_transformers`` to finish loading
    behind a proxy that injects a self-signed root CA:

    1. ``hf_xet`` (HuggingFace's Rust-based chunked transfer, used by default
       for large blobs since ``huggingface_hub`` 0.30+) is disabled via
       ``HF_HUB_DISABLE_XET=1``. xet ships its own HTTP client and does not
       honor Python-level patches, so leaving it on would silently re-hang
       the download even with verify disabled.
    2. ``requests.Session`` is monkey-patched to default ``verify=False``.
       After step 1, the model blob falls back to the plain ``requests``
       path, where this patch actually takes effect.

    Without this, the first model fetch leaves a zero-byte ``.incomplete``
    blob, the FastAPI startup hook never returns, port 8090 never binds, and
    every dashboard/listener health check sees "Connection refused".
    """
    global _TLS_VERIFICATION_PATCHED
    if _TLS_VERIFICATION_PATCHED:
        return

    import os

    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    import requests  # type: ignore
    import urllib3  # type: ignore

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    original_request = requests.Session.request

    def patched_request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("verify", False)
        return original_request(self, method, url, **kwargs)

    requests.Session.request = patched_request  # type: ignore[method-assign]
    _TLS_VERIFICATION_PATCHED = True


class SentenceTransformersEmbedder:
    """Embedder implementation that loads a HuggingFace sentence-transformer."""

    def __init__(self, settings: "WorkerSettings") -> None:
        if (settings.embedding_tls_verify or "1").strip() == "0":
            _disable_requests_tls_verification()
            logger.warning(
                "EMBEDDING_TLS_VERIFY=0 — TLS verification disabled for "
                "HuggingFace downloads. Set this only behind a trusted "
                "corporate MITM proxy.",
            )

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
