"""Elasticsearch-backed solution repository.

Designed so the analysis graph never imports elasticsearch directly — if
you replace this module with, say, an OpenSearch / Qdrant implementation,
nothing in ``graph.py`` has to change.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ..base import Solution, SolutionMatch, SolutionRecord

if TYPE_CHECKING:
    from elasticsearch import Elasticsearch

    from ...config import WorkerSettings
    from ...embeddings.base import Embedder

logger = logging.getLogger(__name__)


def _build_client(settings: "WorkerSettings") -> "Elasticsearch":
    from elasticsearch import Elasticsearch

    kwargs: dict[str, Any] = {
        "hosts": [settings.elasticsearch_url],
        "request_timeout": settings.elasticsearch_request_timeout_seconds,
    }
    if settings.elasticsearch_api_key:
        kwargs["api_key"] = settings.elasticsearch_api_key
    logger.info("Elasticsearch client hosts=%s", settings.elasticsearch_url)
    return Elasticsearch(**kwargs)


def _combined_rank(hit: dict[str, Any]) -> float:
    src = hit.get("_source") or {}
    base = float(hit.get("_score") or 0.0)
    q = float(src.get("solution_score", 1.0))
    return base * q


class ElasticsearchSolutionRepository:
    """kNN-backed repository of verified past solutions.

    Two-phase: embed the query fingerprint, then run a dense-vector kNN
    against ``fingerprint_vector``. Hits below
    ``settings.similarity_threshold`` are dropped so the analysis graph's
    router falls back to ``analyze_fresh`` for weak neighbours.
    """

    def __init__(
        self,
        settings: "WorkerSettings",
        embedder: "Embedder",
        *,
        index_name: str | None = None,
    ) -> None:
        self._settings = settings
        self._embedder = embedder
        self._index = index_name or settings.elasticsearch_index
        self._threshold = settings.similarity_threshold
        self._top_k = settings.similarity_top_k
        self._num_candidates = settings.similarity_num_candidates
        self._client: Elasticsearch | None = None

    # ── lazy client so tests / CLIs can import without ES running ──

    def _es(self) -> "Elasticsearch":
        if self._client is None:
            self._client = _build_client(self._settings)
        return self._client

    # ── schema ──

    def _mapping(self) -> dict[str, Any]:
        return {
            "fingerprint_text": {"type": "text"},
            "fingerprint_vector": {
                "type": "dense_vector",
                "dims": self._embedder.dimensions,
                "index": True,
                "similarity": "cosine",
            },
            "solution": {"type": "text"},
            "solution_score": {"type": "float"},
            "job_name": {"type": "keyword"},
            "stage_name": {"type": "keyword"},
            "build_number": {"type": "integer"},
            "created_at": {"type": "date"},
        }

    def ensure_ready(self) -> None:
        es = self._es()
        if es.indices.exists(index=self._index):
            return
        es.indices.create(index=self._index, mappings={"properties": self._mapping()})
        logger.info(
            "Created ES index: %s  dims=%d",
            self._index,
            self._embedder.dimensions,
        )

    # ── read ──

    def search(self, fingerprint: str) -> list[SolutionMatch]:
        es = self._es()
        vector = self._embedder.embed(fingerprint)
        try:
            resp = es.search(
                index=self._index,
                knn={
                    "field": "fingerprint_vector",
                    "query_vector": vector,
                    "k": max(self._top_k, 5),
                    "num_candidates": self._num_candidates,
                },
                source=[
                    "fingerprint_text",
                    "solution",
                    "solution_score",
                    "job_name",
                    "stage_name",
                    "build_number",
                ],
            )
        except Exception:
            logger.exception("ES knn search failed")
            return []

        ranked = [
            h
            for h in resp["hits"]["hits"]
            if float(h.get("_score") or 0.0) >= self._threshold
        ]
        ranked.sort(key=_combined_rank, reverse=True)

        out: list[SolutionMatch] = []
        for h in ranked[: self._top_k]:
            src = h.get("_source") or {}
            out.append(
                SolutionMatch(
                    score=float(h.get("_score") or 0.0),
                    solution=str(src.get("solution", "")),
                    fingerprint_text=str(src.get("fingerprint_text", "")),
                    job_name=str(src.get("job_name", "")),
                    stage_name=str(src.get("stage_name", "")),
                    build_number=int(src.get("build_number") or 0),
                    raw=src,
                ),
            )
        return out

    # ── write ──

    def store(self, solution: Solution, *, doc_id: str | None = None) -> str:
        """Insert / overwrite a solution doc.

        When ``doc_id`` is provided, ES treats the call as upsert-by-id, so
        re-stores from the same Postgres session collapse to a single kNN
        entry rather than accumulating duplicates. When omitted, fall back
        to a fresh UUID for backward compatibility.
        """
        es = self._es()
        resolved_id = (doc_id or "").strip() or str(uuid.uuid4())
        es.index(
            index=self._index,
            id=resolved_id,
            document={
                "fingerprint_text": solution.fingerprint_text,
                "fingerprint_vector": self._embedder.embed(solution.fingerprint_text),
                "solution": solution.solution,
                "solution_score": solution.solution_score,
                "job_name": solution.job_name,
                "stage_name": solution.stage_name,
                "build_number": solution.build_number,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        logger.info(
            "Stored solution doc_id=%s for %s #%d",
            resolved_id,
            solution.job_name,
            solution.build_number,
        )
        return resolved_id

    # ── bulk read (knowledge-graph) ──

    def list_all(
        self, *, limit: int = 2000, include_vectors: bool = False,
    ) -> list[SolutionRecord]:
        """Return up to ``limit`` solution docs.

        Used by the knowledge-graph endpoint to materialise the full corpus.
        ``include_vectors=True`` ships the persisted dense vectors so callers
        can compute similarity edges client-side without re-embedding every
        fingerprint.
        """
        es = self._es()
        try:
            if not es.indices.exists(index=self._index):
                return []
        except Exception:
            logger.exception("ES exists check failed for %s", self._index)
            return []

        source: list[str] = [
            "fingerprint_text",
            "solution",
            "solution_score",
            "job_name",
            "stage_name",
            "build_number",
            "created_at",
        ]
        if include_vectors:
            source.append("fingerprint_vector")

        try:
            resp = es.search(
                index=self._index,
                size=max(1, min(int(limit), 10_000)),
                query={"match_all": {}},
                sort=[{"created_at": {"order": "desc", "unmapped_type": "date"}}],
                source=source,
            )
        except Exception:
            logger.exception("ES list_all failed")
            return []

        out: list[SolutionRecord] = []
        for h in resp.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            vec_raw = src.get("fingerprint_vector") if include_vectors else None
            vec: list[float] = []
            if vec_raw:
                try:
                    vec = [float(x) for x in vec_raw]
                except (TypeError, ValueError):
                    vec = []
            out.append(
                SolutionRecord(
                    doc_id=str(h.get("_id") or ""),
                    fingerprint_text=str(src.get("fingerprint_text", "")),
                    solution=str(src.get("solution", "")),
                    job_name=str(src.get("job_name", "")),
                    stage_name=str(src.get("stage_name", "")),
                    build_number=int(src.get("build_number") or 0),
                    solution_score=float(src.get("solution_score") or 1.0),
                    created_at=str(src.get("created_at", "")),
                    vector=vec,
                ),
            )
        return out

    # ── retention ──

    def prune(self, older_than_days: int) -> None:
        if older_than_days <= 0:
            return
        es = self._es()
        try:
            if not es.indices.exists(index=self._index):
                return
        except Exception:
            logger.exception("ES exists check failed for %s", self._index)
            return
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            es.delete_by_query(
                index=self._index,
                query={"range": {"created_at": {"lt": cutoff_s}}},
                refresh=True,
                conflicts="proceed",
            )
            logger.info("Pruned documents in %s older than %s", self._index, cutoff_s)
        except Exception:
            logger.exception("Prune failed for index %s", self._index)
