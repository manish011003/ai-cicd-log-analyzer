"""Protocol and value objects for the solution repository.

The protocol is intentionally tiny — every backend must implement the same
four operations (ensure schema, search, store, prune) so swapping
Elasticsearch for OpenSearch / Qdrant / pgvector is a one-file change in
``providers/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class Solution:
    """Payload written to the vector store when an operator accepts a fix."""

    fingerprint_text: str
    solution: str
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    solution_score: float = 1.0


@dataclass
class SolutionMatch:
    """Search hit returned by :meth:`SolutionRepository.search`."""

    score: float
    solution: str
    fingerprint_text: str
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "solution": self.solution,
            "fingerprint_text": self.fingerprint_text,
            "job_name": self.job_name,
            "stage_name": self.stage_name,
            "build_number": self.build_number,
        }


@dataclass
class SolutionRecord:
    """Full record returned by :meth:`SolutionRepository.list_all`.

    Carries the persisted embedding so callers (e.g. the knowledge-graph
    endpoint) can compute similarity edges without re-embedding every
    fingerprint.
    """

    doc_id: str
    fingerprint_text: str
    solution: str
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    solution_score: float = 1.0
    created_at: str = ""
    vector: list[float] = field(default_factory=list)


@runtime_checkable
class SolutionRepository(Protocol):
    """Persistent store of verified past solutions, keyed by fingerprint embeddings."""

    def ensure_ready(self) -> None: ...  # pragma: no cover - protocol

    def search(self, fingerprint: str) -> list[SolutionMatch]: ...  # pragma: no cover - protocol

    def store(
        self, solution: Solution, *, doc_id: str | None = None,
    ) -> str: ...  # pragma: no cover - protocol

    def prune(self, older_than_days: int) -> None: ...  # pragma: no cover - protocol

    def list_all(
        self, *, limit: int = 2000, include_vectors: bool = False,
    ) -> list[SolutionRecord]: ...  # pragma: no cover - protocol
