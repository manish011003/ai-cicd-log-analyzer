"""Pure-function tests for jenkins_client helpers (no HTTP, no Postgres).

Run from the repo root:

    pytest jenkins_failure_listener/tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Inject the listener package onto sys.path so `from app...` imports resolve
# regardless of whether the user invokes pytest from the repo root or the
# listener subdirectory.
_LISTENER_ROOT = Path(__file__).resolve().parent.parent
if str(_LISTENER_ROOT) not in sys.path:
    sys.path.insert(0, str(_LISTENER_ROOT))

# Provide harmless defaults for required env vars before importing app modules.
os.environ.setdefault("JENKINS_BASE_URL", "http://example.invalid")
os.environ.setdefault("JENKINS_USER", "u")
os.environ.setdefault("JENKINS_API_TOKEN", "t")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("WORKER_INGEST_URL", "http://example.invalid/ingest/failure")
os.environ.setdefault("WORKER_INGEST_API_KEY", "t")

from app.jenkins_client import (  # noqa: E402
    JenkinsClient,
    _cluster_sorted_indices,
    _score_line_as_error_anchor,
    _strip_html,
    _truncate_log_chars,
)


def test_parse_title_with_paren_failed():
    parsed = JenkinsClient._parse_title_and_link(
        "team » web-build #123 (Failed)",
        "http://jenkins/job/team/job/web-build/123/",
    )
    assert parsed == {
        "job_full_name": "team/web-build",
        "build_number": 123,
        "build_url": "http://jenkins/job/team/job/web-build/123/",
    }


def test_parse_title_without_trailing_clause():
    parsed = JenkinsClient._parse_title_and_link(
        "demo #7",
        "http://jenkins/job/demo/7/",
    )
    assert parsed is not None
    assert parsed["job_full_name"] == "demo"
    assert parsed["build_number"] == 7


def test_parse_title_unparseable_returns_none():
    assert JenkinsClient._parse_title_and_link("not a build line", "http://x/") is None


def test_strip_html_removes_tags_and_jenkins_timestamp_prefix():
    raw = "<span>10:11:12 hello</span>\n12:13:14 world&amp;done"
    out = _strip_html(raw)
    assert "<" not in out and ">" not in out
    assert "hello" in out and "world&done" in out
    assert "10:11:12" not in out and "12:13:14" not in out


def test_truncate_log_chars_keeps_head_and_tail():
    text = "A" * 1000 + "B" * 1000
    out = _truncate_log_chars(text, 600)
    assert len(out) <= 600
    # Both ends should appear; the middle is replaced by an omission notice.
    assert out.startswith("A")
    assert out.rstrip().endswith("B")
    assert "characters omitted" in out


def test_score_line_as_error_anchor_prefers_typed_exception():
    score_exc = _score_line_as_error_anchor(
        "ERROR java.lang.NullPointerException at com.example.Foo.bar(Foo.java:42)",
    )
    score_plain = _score_line_as_error_anchor("ERROR build failed during compile step")
    assert score_exc is not None and score_plain is not None
    assert score_exc > score_plain


def test_score_line_returns_none_for_neutral_line():
    assert _score_line_as_error_anchor("just some informational text") is None


def test_cluster_sorted_indices_merges_close_indices():
    out = _cluster_sorted_indices([1, 2, 3, 9, 10, 25], merge_gap=2)
    assert out == [(1, 3), (9, 10), (25, 25)]


def test_cluster_sorted_indices_handles_empty():
    assert _cluster_sorted_indices([], merge_gap=3) == []
