"""Unit tests for :mod:`failure_analyzer_worker.knowledge_graph`."""

from __future__ import annotations

from failure_analyzer_worker.knowledge_graph import build_knowledge_graph
from failure_analyzer_worker.vectorstore.base import SolutionRecord


def _rec(
    doc_id: str,
    fingerprint: str,
    job: str,
    stage: str,
    vector: list[float],
    *,
    solution: str = "fix it",
) -> SolutionRecord:
    return SolutionRecord(
        doc_id=doc_id,
        fingerprint_text=fingerprint,
        solution=solution,
        job_name=job,
        stage_name=stage,
        build_number=1,
        solution_score=1.0,
        created_at="2026-05-01T00:00:00Z",
        vector=vector,
    )


def test_empty_corpus_returns_zero_stats():
    out = build_knowledge_graph([])
    assert out["nodes"] == []
    assert out["edges"] == []
    assert out["stats"]["total_solutions"] == 0
    assert out["stats"]["communities"] == 0


def test_typed_nodes_and_edges_are_emitted_per_solution():
    records = [
        _rec("a", "OutOfMemoryError: heap", "team/svc", "Build", [1.0, 0.0, 0.0]),
        _rec("b", "TimeoutException: read timed out", "team/svc", "Test", [0.0, 1.0, 0.0]),
    ]
    out = build_knowledge_graph(records, similarity_threshold=0.99)

    kinds = {n["kind"] for n in out["nodes"]}
    assert kinds == {"solution", "error_class", "job", "stage"}

    edge_kinds = {e["kind"] for e in out["edges"]}
    # No similarity edges because the two vectors are orthogonal.
    assert "similar" not in edge_kinds
    assert "resolves" in edge_kinds
    assert "occurs_in" in edge_kinds
    assert "in_job" in edge_kinds

    assert out["stats"]["total_solutions"] == 2
    assert out["stats"]["total_jobs"] == 1
    assert out["stats"]["total_stages"] == 2
    assert out["stats"]["isolated_solutions"] == 2


def test_similarity_edges_cluster_solutions_into_one_community():
    records = [
        _rec("a", "OutOfMemoryError #1", "team/svc", "Build", [1.0, 0.0, 0.0]),
        _rec("b", "OutOfMemoryError #2", "team/svc", "Build", [0.99, 0.01, 0.0]),
        _rec("c", "OutOfMemoryError #3", "team/svc", "Build", [0.98, 0.02, 0.0]),
        _rec("d", "Unrelated", "team/svc", "Test", [0.0, 1.0, 0.0]),
    ]
    out = build_knowledge_graph(records, similarity_threshold=0.9, max_neighbours=4)

    sim_edges = [e for e in out["edges"] if e["kind"] == "similar"]
    assert len(sim_edges) >= 2  # a-b, b-c at minimum

    # Three OOM solutions should land in one community; the orthogonal one alone.
    sizes = sorted(c["size"] for c in out["communities"])
    assert sizes == [1, 3]

    god = out["stats"]["god_nodes"][0]
    assert god["degree"] >= 1
    assert "OutOfMemoryError" in god["error_class"]


def test_records_without_vectors_still_get_typed_edges():
    records = [
        _rec("a", "OutOfMemoryError", "team/svc", "Build", []),
        _rec("b", "TimeoutException", "team/svc", "Test", []),
    ]
    out = build_knowledge_graph(records)

    assert out["stats"]["total_solutions"] == 2
    assert out["stats"]["total_similarity_edges"] == 0
    # Typed edges still emitted even without similarity edges.
    assert any(e["kind"] == "resolves" for e in out["edges"])
