"""Backward-compat shim — prefer :mod:`failure_analyzer_worker.filtering` and
:mod:`failure_analyzer_worker.vectorstore` in new code.

Reason this module still exists:
  * ``tests/test_log_processor.py`` imports ``filter_logs``, ``generate_fingerprint``,
    ``LogProcessor`` and ``reset_es_client`` from here.
  * ``try_worker.py`` imports ``search_similar_solutions`` and ``settings`` from here.

Everything below is a thin delegate; retire this file once those callers
are migrated. The old context-repository helpers (``store_filtered_context``,
``ensure_context_index``) were dropped when the ``failure_context`` ES index
was removed — accepted solutions are the only thing we persist now.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import settings
from .filtering import (
    LogProcessor,
    filter_logs,
    generate_fingerprint,
    primary_error,
)

__all__ = [
    "LogProcessor",
    "embed_text",
    "ensure_index",
    "filter_logs",
    "generate_fingerprint",
    "primary_error",
    "prune_stale_documents",
    "reset_es_client",
    "search_similar_solutions",
    "settings",
    "store_solution",
]

logger = logging.getLogger(__name__)


# ── Default singletons for legacy callers (try_worker.py and old tests) ──────
#
# New code should build ``Deps`` in the FastAPI lifespan and pass the
# embedder / repository down explicitly, as the worker does. These
# module-level helpers exist purely to not break outside-the-app entry points.

_embedder = None  # failure_analyzer_worker.embeddings.base.Embedder | None
_solution_repo = None  # failure_analyzer_worker.vectorstore.base.SolutionRepository | None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from .embeddings import create_embedder

        _embedder = create_embedder(settings)
    return _embedder


def _get_solution_repo():
    global _solution_repo
    if _solution_repo is None:
        from .vectorstore import create_solution_repository

        _solution_repo = create_solution_repository(settings, _get_embedder())
    return _solution_repo


# ── Legacy function-style façade ─────────────────────────────────────────────


def reset_es_client() -> None:
    """Drop cached singletons (tests + post-config reloads)."""
    global _embedder, _solution_repo
    _embedder = None
    _solution_repo = None


def embed_text(text: str) -> list[float]:
    return _get_embedder().embed(text)


def ensure_index() -> None:
    _get_solution_repo().ensure_ready()


def search_similar_solutions(fingerprint: str) -> list[dict[str, Any]]:
    matches = _get_solution_repo().search(fingerprint)
    return [m.to_dict() for m in matches]


def store_solution(
    fingerprint: str,
    solution: str,
    job_name: str = "",
    stage_name: str = "",
    build_number: int = 0,
    solution_score: float = 1.0,
) -> str:
    from .vectorstore.base import Solution

    return _get_solution_repo().store(
        Solution(
            fingerprint_text=fingerprint,
            solution=solution,
            job_name=job_name,
            stage_name=stage_name,
            build_number=build_number,
            solution_score=solution_score,
        ),
    )


def prune_stale_documents() -> None:
    _get_solution_repo().prune(settings.elasticsearch_retention_solutions_days)
