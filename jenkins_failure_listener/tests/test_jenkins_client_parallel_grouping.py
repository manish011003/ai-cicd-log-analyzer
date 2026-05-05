"""Tests for parallel-stage grouping in :class:`JenkinsClient`.

Two paths under test:

1. ``_group_by_flow_graph_parallel_blocks`` — authoritative grouping by
   walking the wfapi node ids between top-level stages and matching
   ``Execute in parallel : Start`` / ``: End`` boundary markers.
2. ``_group_by_time_overlap_with_slack`` — backup grouping by
   execution-time overlap with a configurable slack budget.

The integration glue (``_select_root_failure_stages``) is also exercised
to confirm the primary→backup fallthrough triggers on lookup errors.

Run from the repo root:

    pytest jenkins_failure_listener/tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_LISTENER_ROOT = Path(__file__).resolve().parent.parent
if str(_LISTENER_ROOT) not in sys.path:
    sys.path.insert(0, str(_LISTENER_ROOT))

os.environ.setdefault("JENKINS_BASE_URL", "http://example.invalid")
os.environ.setdefault("JENKINS_USER", "u")
os.environ.setdefault("JENKINS_API_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("WORKER_INGEST_URL", "http://example.invalid/ingest/failure")
os.environ.setdefault("WORKER_INGEST_API_KEY", "t")

from app.jenkins_client import JenkinsClient  # noqa: E402


def _stage(
    stage_id: int,
    name: str,
    start: int,
    duration: int,
    status: str = "SUCCESS",
) -> dict:
    return {
        "id": stage_id,
        "name": name,
        "status": status,
        "startTimeMillis": start,
        "durationMillis": duration,
    }


def _client(**overrides: Any) -> JenkinsClient:
    kwargs: dict[str, Any] = {
        "base_url": "http://example.invalid",
        "user": "u",
        "api_token": "t",
        "failed_rss_path": "/rssFailed",
    }
    kwargs.update(overrides)
    return JenkinsClient(**kwargs)


class _FakeResponse:
    """httpx.Response stand-in that only exposes the bits our code uses."""

    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    """Stub for ``JenkinsClient.client`` that serves canned wfapi responses.

    ``names_by_id`` maps integer node ids to the ``name`` string Jenkins
    would return for that node's ``wfapi/describe`` payload. Anything
    not in the map returns 404, which the code treats as "no marker
    here" — the same behaviour as a node id that's been skipped.
    """

    def __init__(
        self,
        names_by_id: dict[int, str],
        *,
        fail_on_id: int | None = None,
    ) -> None:
        self._names = names_by_id
        self._fail_on_id = fail_on_id
        self.calls: list[int] = []

    def get(self, url: str) -> _FakeResponse:
        # URL ends in ``…/execution/node/<id>/wfapi/describe`` — the node id
        # is the third-from-last path segment.
        parts = url.rstrip("/").split("/")
        nid = int(parts[-3])
        self.calls.append(nid)
        if self._fail_on_id is not None and nid == self._fail_on_id:
            return _FakeResponse(status_code=500)
        if nid not in self._names:
            return _FakeResponse(status_code=404)
        return _FakeResponse(status_code=200, payload={"name": self._names[nid]})


# ── _group_by_time_overlap_with_slack ────────────────────────────────────────


def test_overlap_with_slack_groups_delayed_sibling_within_slack():
    """Sibling B starts 1500 ms after A finishes — slack 2000 → same group."""
    a = _stage(10, "A", start=1_000, duration=400, status="FAILED")
    b = _stage(20, "B", start=1_400 + 1_500, duration=400, status="FAILED")
    groups = JenkinsClient._group_by_time_overlap_with_slack([a, b], slack_ms=2000)
    assert len(groups) == 1
    assert {s["id"] for s in groups[0]} == {10, 20}


def test_overlap_with_slack_splits_when_gap_exceeds_slack():
    """Sibling B starts 3000 ms after A finishes — slack 2000 → separate groups."""
    a = _stage(10, "A", start=1_000, duration=400)
    b = _stage(20, "B", start=1_400 + 3_000, duration=400, status="FAILED")
    groups = JenkinsClient._group_by_time_overlap_with_slack([a, b], slack_ms=2000)
    assert len(groups) == 2
    assert [g[0]["id"] for g in groups] == [10, 20]


def test_overlap_with_slack_zero_matches_strict_behaviour():
    """``slack_ms=0`` should reproduce the strict ``s_start < group_end`` rule."""
    a = _stage(10, "A", start=1_000, duration=400)
    b = _stage(20, "B", start=1_400, duration=400)  # touches A's end exactly
    groups = JenkinsClient._group_by_time_overlap_with_slack([a, b], slack_ms=0)
    assert len(groups) == 2  # not overlapping under strict <


def test_overlap_with_slack_handles_empty_list():
    assert JenkinsClient._group_by_time_overlap_with_slack([], slack_ms=2000) == []


# ── _group_by_flow_graph_parallel_blocks ─────────────────────────────────────


def test_flow_graph_groups_siblings_by_parallel_markers():
    """Stages 92, 94, 96 sit inside a `parallel` block opened at node 81."""
    stages = [
        _stage(80, "Build modules", start=1_000, duration=150),
        _stage(92, "config-server", start=1_200, duration=20_000, status="FAILED"),
        _stage(94, "discovery", start=1_300, duration=10_000, status="FAILED"),
        _stage(96, "api-gateway", start=1_400, duration=15_000),
        _stage(165, "Publish", start=22_000, duration=70, status="FAILED"),
    ]
    names = {
        81: "Execute in parallel : Start",
        91: "Stage : Start",
        93: "Stage : Start",
        95: "Stage : Start",
        160: "Execute in parallel : End",
    }
    client = _client()
    client.client = _FakeHttpClient(names)  # type: ignore[assignment]

    groups = client._group_by_flow_graph_parallel_blocks("demo", 1, stages)

    assert groups is not None
    flat = [{s["id"] for s in g} for g in groups]
    # Stage 80 is sequential (singleton), 92/94/96 share parallel block 81,
    # 165 is the cascade after the block ends.
    assert {80} in flat
    assert {92, 94, 96} in flat
    assert {165} in flat


def test_flow_graph_returns_none_on_lookup_failure():
    """A 5xx response on any probe must trigger the backup path."""
    stages = [
        _stage(10, "A", start=0, duration=10),
        _stage(20, "B", start=20, duration=10),
    ]
    client = _client()
    client.client = _FakeHttpClient({}, fail_on_id=15)  # type: ignore[assignment]

    assert client._group_by_flow_graph_parallel_blocks("demo", 1, stages) is None


def test_flow_graph_returns_none_when_open_block_never_closes():
    """An open parallel block whose End sits outside the edge window must
    trigger the surrender path so the slack backup can take over.

    The Start marker is found in stage A's forward edge, but no End
    appears in any subsequent edge window (only a non-boundary 'Stage :
    Start' near stage B). After processing every stage the stack still
    holds the unmatched open block, so the function returns None.
    """
    stages = [
        _stage(10, "A", start=0, duration=10),
        _stage(40, "B", start=20, duration=10),
    ]
    names = {
        11: "Execute in parallel : Start",
        # End would live at e.g. id 25 in real Jenkins — deliberately
        # absent here so we can prove the sanity check fires.
        39: "Stage : Start",
    }
    client = _client(parallel_block_edge_scan_ids=4)
    client.client = _FakeHttpClient(names)  # type: ignore[assignment]

    assert client._group_by_flow_graph_parallel_blocks("demo", 1, stages) is None


def test_flow_graph_skips_backward_scan_when_no_block_open():
    """Sequential-only builds should never trigger a backward edge scan
    (no End is possible when the stack is empty), keeping HTTP cost
    minimal.
    """
    stages = [
        _stage(10, "A", start=0, duration=10),
        _stage(50, "B", start=20, duration=10, status="FAILED"),
    ]
    fake = _FakeHttpClient({})  # all 404s — no markers anywhere
    client = _client(parallel_block_edge_scan_ids=8)
    client.client = fake  # type: ignore[assignment]

    groups = client._group_by_flow_graph_parallel_blocks("demo", 1, stages)
    assert groups is not None
    assert [[s["id"] for s in g] for g in groups] == [[10], [50]]
    # The backward window for stage B would be ids 42..49 — they must
    # never be probed because no parallel block is open on the stack.
    assert not any(42 <= nid < 50 for nid in fake.calls)


def test_flow_graph_purely_sequential_build_yields_singletons():
    """No parallel markers anywhere → each stage is its own group, in order."""
    stages = [
        _stage(10, "A", start=0, duration=100),
        _stage(20, "B", start=200, duration=100, status="FAILED"),
        _stage(30, "C", start=400, duration=100),
    ]
    client = _client()
    client.client = _FakeHttpClient({})  # type: ignore[assignment]

    groups = client._group_by_flow_graph_parallel_blocks("demo", 1, stages)
    assert groups is not None
    assert [[s["id"] for s in g] for g in groups] == [[10], [20], [30]]


# ── _select_root_failure_stages (integration) ────────────────────────────────


def test_select_root_failures_uses_flow_graph_when_available():
    """Primary path: parallel siblings are returned together as the root group."""
    stages = [
        _stage(80, "Build modules", start=1_000, duration=150),
        _stage(92, "config-server", start=1_200, duration=20_000, status="FAILED"),
        _stage(94, "discovery", start=1_300, duration=10_000, status="FAILED"),
        _stage(165, "Publish", start=22_000, duration=70, status="FAILED"),
    ]
    names = {
        81: "Execute in parallel : Start",
        91: "Stage : Start",
        93: "Stage : Start",
        160: "Execute in parallel : End",
    }
    client = _client()
    client.client = _FakeHttpClient(names)  # type: ignore[assignment]

    failed = client._select_root_failure_stages("demo", 1, stages)
    assert {s["id"] for s in failed} == {92, 94}  # 165 is a downstream cascade


def test_select_root_failures_falls_back_to_overlap_with_slack():
    """When wfapi probes fail, the slack-based grouping still recovers siblings."""
    # Sibling B finishes 500 ms before C starts — strict < would split them,
    # but the default 2 s slack on the backup path keeps them together.
    a = _stage(10, "A", start=0, duration=200, status="SUCCESS")
    b = _stage(20, "B", start=300, duration=400, status="FAILED")
    c = _stage(30, "C", start=1_200, duration=400, status="FAILED")
    client = _client(parallel_stage_overlap_ms=2_000)
    client.client = _FakeHttpClient({}, fail_on_id=15)  # type: ignore[assignment]

    failed = client._select_root_failure_stages("demo", 1, [a, b, c])
    assert {s["id"] for s in failed} == {20, 30}


def test_select_root_failures_empty_stages_returns_empty():
    client = _client()
    client.client = _FakeHttpClient({})  # type: ignore[assignment]
    assert client._select_root_failure_stages("demo", 1, []) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
