"""Build a typed knowledge graph from the accepted-solutions corpus.

Inputs come from :class:`SolutionRepository.list_all` (the only ES-shaped
dependency lives there); everything below is pure Python + numpy so the
worker can be tested without a live Elasticsearch.

Graph shape
~~~~~~~~~~~

Nodes (typed by the ``kind`` field):

* ``solution``    — one per accepted fix in the vector store
* ``error_class`` — extracted from each fingerprint (``OutOfMemoryError`` …)
* ``job``         — Jenkins job full name
* ``stage``       — Jenkins pipeline stage name

Edges (typed by ``kind``):

* ``similar``  solution ↔ solution   (cosine similarity over fingerprint
  vectors, top-K neighbours per node above ``threshold``)
* ``resolves`` solution → error_class
* ``occurs_in`` solution → stage (owned by job)
* ``in_job``   stage → job

Communities are weakly-connected components over the ``similar`` subgraph
— good enough to surface "clusters of related root causes" without
pulling in a Louvain / Leiden dependency.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from typing import Any

from .vectorstore.base import SolutionRecord

logger = logging.getLogger(__name__)


_ERROR_CLASS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Exception|Error|Failure))\b"),
    re.compile(r"\b(TimeoutError|OutOfMemoryError|ConnectionRefused)\b", re.IGNORECASE),
)


def _extract_error_class(text: str) -> str:
    """Pull a stable error class label out of a fingerprint / solution body.

    Mirrors the heuristic used by ``web/backend/app/main.py:_extract_error_class``
    so the graph nodes line up with what the dashboard already displays.
    """
    for pat in _ERROR_CLASS_PATTERNS:
        m = pat.search(text or "")
        if m:
            token = m.group(1) if m.lastindex else m.group(0)
            return token[:80]
    head = (text or "").strip().splitlines()[:1]
    if head:
        snippet = head[0].strip()
        if snippet:
            return snippet[:60]
    return "UnknownError"


# ── numpy is a transitive dep of sentence-transformers + elasticsearch, so we
#    rely on it for the cosine block; fall back to pure-Python for tiny inputs
#    (or if numpy is somehow missing) so unit tests don't need it either.

def _normalise_pure(vectors: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for v in vectors:
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


def _cosine_top_k_pure(
    vectors: list[list[float]], k: int, threshold: float,
) -> list[list[tuple[int, float]]]:
    n = len(vectors)
    if n == 0:
        return []
    normed = _normalise_pure(vectors)
    out: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i in range(n):
        scored: list[tuple[int, float]] = []
        a = normed[i]
        for j in range(n):
            if i == j:
                continue
            b = normed[j]
            sim = sum(x * y for x, y in zip(a, b, strict=False))
            if sim >= threshold:
                scored.append((j, sim))
        scored.sort(key=lambda kv: kv[1], reverse=True)
        out[i] = scored[:k]
    return out


def _cosine_top_k(
    vectors: list[list[float]], k: int, threshold: float,
) -> list[list[tuple[int, float]]]:
    """Return per-node top-K neighbours with sim >= ``threshold``.

    Uses numpy when available for the O(N²) block; the pure-Python fallback
    keeps unit tests dependency-light.
    """
    if not vectors:
        return []
    try:
        import numpy as np
    except ImportError:
        return _cosine_top_k_pure(vectors, k, threshold)

    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] == 0:
        return []
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    normed = arr / norms
    sim = normed @ normed.T
    np.fill_diagonal(sim, -1.0)  # exclude self-hits

    out: list[list[tuple[int, float]]] = []
    for i in range(sim.shape[0]):
        row = sim[i]
        idx = np.where(row >= threshold)[0]
        if idx.size == 0:
            out.append([])
            continue
        scored = [(int(j), float(row[j])) for j in idx]
        scored.sort(key=lambda kv: kv[1], reverse=True)
        out.append(scored[:k])
    return out


def _connected_components(
    n: int, adjacency: list[list[int]],
) -> list[int]:
    """Standard union-find over the ``similar`` subgraph; returns ``community_id`` per node."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i, neigh in enumerate(adjacency):
        for j in neigh:
            union(i, j)

    root_to_id: dict[int, int] = {}
    out: list[int] = []
    for i in range(n):
        r = find(i)
        if r not in root_to_id:
            root_to_id[r] = len(root_to_id)
        out.append(root_to_id[r])
    return out


def build_knowledge_graph(
    records: list[SolutionRecord],
    *,
    similarity_threshold: float = 0.75,
    max_neighbours: int = 6,
) -> dict[str, Any]:
    """Build the typed graph + summary metrics for the dashboard.

    Returns a JSON-serialisable dict with ``nodes``, ``edges``, ``communities``
    (sorted by size) and ``stats`` (god nodes, isolated count, totals).
    """
    n = len(records)
    if n == 0:
        return {
            "nodes": [],
            "edges": [],
            "communities": [],
            "stats": {
                "total_solutions": 0,
                "total_error_classes": 0,
                "total_jobs": 0,
                "total_stages": 0,
                "total_similarity_edges": 0,
                "isolated_solutions": 0,
                "communities": 0,
                "largest_community_size": 0,
                "avg_neighbours": 0.0,
                "god_nodes": [],
                "top_error_classes": [],
            },
            "params": {
                "similarity_threshold": similarity_threshold,
                "max_neighbours": max_neighbours,
            },
        }

    # ── solution nodes + side dictionaries ─────────────────────────────────
    solution_node_ids: list[str] = []
    nodes: list[dict[str, Any]] = []
    error_class_for: list[str] = []
    error_class_node_ids: dict[str, str] = {}
    job_node_ids: dict[str, str] = {}
    stage_node_ids: dict[tuple[str, str], str] = {}

    def _nid(prefix: str, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "_"
        return f"{prefix}:{slug[:120]}"

    for record in records:
        sid = f"sol:{record.doc_id or _nid('sol', record.fingerprint_text or record.solution)}"
        solution_node_ids.append(sid)
        ec = _extract_error_class(record.fingerprint_text or record.solution)
        error_class_for.append(ec)
        nodes.append(
            {
                "id": sid,
                "kind": "solution",
                "label": record.solution.strip().splitlines()[0][:80] if record.solution else ec,
                "doc_id": record.doc_id,
                "fingerprint_text": record.fingerprint_text,
                "solution": record.solution,
                "job_name": record.job_name,
                "stage_name": record.stage_name,
                "build_number": record.build_number,
                "solution_score": record.solution_score,
                "created_at": record.created_at,
                "error_class": ec,
            },
        )

    edges: list[dict[str, Any]] = []

    # ── similarity edges (deduped, undirected) ─────────────────────────────
    vectors = [r.vector for r in records]
    if any(not v for v in vectors):
        # Records without vectors can't participate in similarity scoring;
        # the rest of the graph (typed edges) still works.
        logger.info(
            "knowledge_graph: %d/%d records have no vector; skipping their similarity edges",
            sum(1 for v in vectors if not v),
            n,
        )

    indexed = [(i, v) for i, v in enumerate(vectors) if v]
    sub_idx = [i for i, _ in indexed]
    sub_vecs = [v for _, v in indexed]
    neighbours_sub = _cosine_top_k(sub_vecs, max_neighbours, similarity_threshold)

    adjacency_full: list[list[int]] = [[] for _ in range(n)]
    seen_pair: set[tuple[int, int]] = set()
    for local_i, neigh in enumerate(neighbours_sub):
        i = sub_idx[local_i]
        for local_j, sim in neigh:
            j = sub_idx[local_j]
            a, b = (i, j) if i < j else (j, i)
            if (a, b) in seen_pair:
                continue
            seen_pair.add((a, b))
            adjacency_full[a].append(b)
            adjacency_full[b].append(a)
            edges.append(
                {
                    "kind": "similar",
                    "source": solution_node_ids[a],
                    "target": solution_node_ids[b],
                    "weight": round(sim, 4),
                },
            )

    # ── error_class / job / stage nodes + typed edges ──────────────────────
    for i, record in enumerate(records):
        ec = error_class_for[i]
        if ec not in error_class_node_ids:
            ec_id = _nid("err", ec)
            error_class_node_ids[ec] = ec_id
            nodes.append(
                {"id": ec_id, "kind": "error_class", "label": ec},
            )
        edges.append(
            {
                "kind": "resolves",
                "source": solution_node_ids[i],
                "target": error_class_node_ids[ec],
            },
        )

        job = (record.job_name or "").strip()
        stage = (record.stage_name or "").strip()
        if job:
            if job not in job_node_ids:
                jid = _nid("job", job)
                job_node_ids[job] = jid
                nodes.append({"id": jid, "kind": "job", "label": job})
        if stage:
            key = (job, stage)
            if key not in stage_node_ids:
                stid = _nid("stage", f"{job}__{stage}" if job else stage)
                stage_node_ids[key] = stid
                nodes.append(
                    {
                        "id": stid,
                        "kind": "stage",
                        "label": stage,
                        "job_name": job,
                    },
                )
                if job:
                    edges.append(
                        {
                            "kind": "in_job",
                            "source": stage_node_ids[key],
                            "target": job_node_ids[job],
                        },
                    )
            edges.append(
                {
                    "kind": "occurs_in",
                    "source": solution_node_ids[i],
                    "target": stage_node_ids[key],
                },
            )

    # ── communities + degree centrality (over similarity subgraph only) ───
    community_ids = _connected_components(n, adjacency_full)
    by_community: dict[int, list[int]] = defaultdict(list)
    for idx, cid in enumerate(community_ids):
        by_community[cid].append(idx)

    communities = [
        {
            "id": cid,
            "size": len(members),
            "members": [solution_node_ids[i] for i in members],
            "label": _community_label(records, members),
        }
        for cid, members in sorted(
            by_community.items(), key=lambda kv: len(kv[1]), reverse=True,
        )
    ]

    degree = [len(neigh) for neigh in adjacency_full]
    god_nodes = sorted(
        (
            {
                "id": solution_node_ids[i],
                "degree": degree[i],
                "label": nodes[i]["label"],
                "error_class": error_class_for[i],
            }
            for i in range(n)
        ),
        key=lambda x: x["degree"],
        reverse=True,
    )[:5]

    isolated_solutions = sum(1 for d in degree if d == 0)

    error_counts: dict[str, int] = defaultdict(int)
    for ec in error_class_for:
        error_counts[ec] += 1
    top_error_classes = [
        {"error_class": ec, "count": cnt}
        for ec, cnt in sorted(error_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ]

    sim_edge_count = sum(1 for e in edges if e["kind"] == "similar")
    avg_neighbours = (sum(degree) / n) if n else 0.0

    # Annotate nodes with community_id + degree so the frontend can colour /
    # size them without re-walking the edge list.
    for i in range(n):
        nodes[i]["community_id"] = community_ids[i]
        nodes[i]["degree"] = degree[i]

    return {
        "nodes": nodes,
        "edges": edges,
        "communities": communities,
        "stats": {
            "total_solutions": n,
            "total_error_classes": len(error_class_node_ids),
            "total_jobs": len(job_node_ids),
            "total_stages": len(stage_node_ids),
            "total_similarity_edges": sim_edge_count,
            "isolated_solutions": isolated_solutions,
            "communities": len(communities),
            "largest_community_size": communities[0]["size"] if communities else 0,
            "avg_neighbours": round(avg_neighbours, 2),
            "god_nodes": god_nodes,
            "top_error_classes": top_error_classes,
        },
        "params": {
            "similarity_threshold": similarity_threshold,
            "max_neighbours": max_neighbours,
        },
    }


def _community_label(records: list[SolutionRecord], members: list[int]) -> str:
    """Pick a representative label for a community (most common error class)."""
    if not members:
        return ""
    counts: dict[str, int] = defaultdict(int)
    for i in members:
        ec = _extract_error_class(records[i].fingerprint_text or records[i].solution)
        counts[ec] += 1
    return max(counts.items(), key=lambda kv: kv[1])[0]
