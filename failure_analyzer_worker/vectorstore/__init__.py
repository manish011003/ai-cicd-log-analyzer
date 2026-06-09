"""Pluggable vector-store / solution-repository layer.

The analysis graph only talks to :class:`SolutionRepository`. The concrete
backend (Elasticsearch today; OpenSearch / Qdrant / pgvector tomorrow) is
wired by :func:`create_solution_repository`.

The previous "context index" (a second ES index that stored filtered log
excerpts) has been removed — the fingerprint is the retrieval key and the
accepted solution is the only payload we ever need to reuse. Raw log
excerpts live in Postgres alongside their sessions.
"""

from .base import Solution, SolutionMatch, SolutionRecord, SolutionRepository
from .factory import create_solution_repository

__all__ = [
    "Solution",
    "SolutionMatch",
    "SolutionRecord",
    "SolutionRepository",
    "create_solution_repository",
]
