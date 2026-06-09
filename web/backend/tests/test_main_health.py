"""Smoke tests for the web-backend FastAPI app.

These tests stub out the Postgres lifespan hook so we can exercise pure-
function helpers and endpoints that don't actually touch the database.

Run from the repo root:

    pytest web/backend/tests -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Allow `from app...` imports regardless of the cwd.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

# The sibling `jenkins_failure_listener` package also exposes a top-level
# `app` module; if its tests ran first in the same interpreter, evict the
# cached entries so we re-import the *web-backend* `app.*` instead.
for _mod_name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod_name, None)

# Required env vars before importing app.config / app.main.
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("WORKER_BASE_URL", "http://worker.invalid:8090")
os.environ.setdefault("WORKER_API_KEY", "test")
os.environ.setdefault("LISTENER_BASE_URL", "http://listener.invalid:8088")

# Patch DB schema bootstrap so the FastAPI lifespan does not try to connect.
with patch("app.db.init_schema", lambda: None):
    from fastapi.testclient import TestClient  # noqa: E402

    from app.main import (  # noqa: E402
        _build_chat_payload,
        _db_target,
        _extract_error_class,
        _summarize_filter_meta,
        _to_result_item,
        app,
    )


def test_health_endpoint_returns_ok():
    with patch("app.db.init_schema", lambda: None):
        with TestClient(app) as client:
            r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_db_target_parses_postgres_url():
    out = _db_target("postgresql://user:pw@db.host:5433/jenkins_listener")
    assert out["scheme"] == "postgresql"
    assert out["host"] == "db.host"
    assert out["port"] == 5433
    assert out["dbname"] == "jenkins_listener"


def test_db_target_handles_garbage():
    # urlparse swallows almost anything, but the helper must never raise.
    out = _db_target("not-a-url")
    assert isinstance(out, dict)


def test_extract_error_class_picks_typed_exception_first():
    row = {
        "fingerprint": "stage:Build | exceptions: ConnectException, IOException",
        "analysis": "irrelevant",
        "suggested_fix": "",
        "filtered_logs": "",
    }
    assert _extract_error_class(row) == "ConnectException"


def test_extract_error_class_falls_back_to_generic_keyword():
    row = {
        "fingerprint": "",
        "analysis": "the build failed during compile",
        "suggested_fix": "",
        "filtered_logs": "",
    }
    assert _extract_error_class(row).lower() in {"failed", "fatal", "error"}


def test_extract_error_class_returns_unknown_for_empty_row():
    assert _extract_error_class({}) == "UnknownError"


def test_to_result_item_normalizes_fields():
    row = {
        "id": "abc-123",
        "job_full_name": "team/web",
        "build_number": 4,
        "stage_name": "Test",
        "fingerprint": "stage:Test | exceptions: AssertionError",
        "filtered_logs": "...",
        "analysis": "boom",
        "suggested_fix": "do this",
        "recommendation": "fresh_analysis",
        "feedback_status": "",
        "created_at": "2026-04-18T00:00:00Z",
    }
    item = _to_result_item(row)
    assert item["run_id"] == "abc-123"
    assert item["job_full_name"] == "team/web"
    assert item["build_number"] == 4
    assert item["stage_name"] == "Test"
    assert item["error_class"] == "AssertionError"
    assert item["feedback_status"] == "new"
    assert item["source"] == "fresh_analysis"


def test_build_chat_payload_returns_blank_template_when_no_row():
    out = _build_chat_payload(None, [{"role": "user", "content": "hi"}])
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["fingerprint"] == ""
    assert out["build_number"] == 0


def test_build_chat_payload_uses_row_metadata():
    row = {
        "fingerprint": "fp",
        "filtered_logs": "logs",
        "job_full_name": "team/web",
        "stage_name": "Build",
        "build_number": 9,
        "analysis": "a",
        "suggested_fix": "f",
    }
    out = _build_chat_payload(row, [{"role": "user", "content": "?"}])
    assert out["job_name"] == "team/web"
    assert out["build_number"] == 9
    assert out["log_excerpt"] == "logs"


# ── Structural-filter telemetry (powers the dashboard Detected panel) ──────


def test_summarize_filter_meta_returns_empty_for_missing_or_legacy_rows():
    assert _summarize_filter_meta(None) == {}
    assert _summarize_filter_meta({}) == {}
    assert _summarize_filter_meta("not-a-dict") == {}


def test_summarize_filter_meta_projects_ui_fields():
    """Pin the trimmed projection the dashboard renders on each FailureCard."""
    meta = {
        "confidence": "high",  # lowercased to verify normalisation
        "activated_detectors": ["java_stack", "generic_shell", ""],
        "primary_location": {
            "kind": "source",
            "file": "PaymentClient.java",
            "line": 84,
            "function": "charge",
            "message": "ConnectException: Connection refused",
        },
        "raw_chars": 12000,
        "body_chars": 1200,
        "body_tokens": 320,
        "selected_count": 12,
        "baseline_version": "self-baseline",
        "collapse_stats": {"banner": 1, "stack-frame-elided": 7},
        # Extra keys must be ignored (forward-compatibility).
        "extra_unrelated": "should-be-dropped",
    }
    out = _summarize_filter_meta(meta)
    assert out["confidence"] == "HIGH"
    assert out["activated_detectors"] == ["java_stack", "generic_shell"]
    assert out["primary_location"]["file"] == "PaymentClient.java"
    assert out["raw_chars"] == 12000
    assert out["body_chars"] == 1200
    assert out["body_tokens"] == 320
    assert out["collapse_stats"] == {"banner": 1, "stack-frame-elided": 7}
    assert "extra_unrelated" not in out


def test_to_result_item_attaches_filter_meta_summary():
    row = {
        "id": "abc-123",
        "job_full_name": "team/web",
        "build_number": 4,
        "stage_name": "Test",
        "fingerprint": "stage:Test | exceptions: AssertionError",
        "filtered_logs": "...",
        "analysis": "boom",
        "suggested_fix": "do this",
        "recommendation": "fresh_analysis",
        "feedback_status": "",
        "created_at": "2026-04-18T00:00:00Z",
        "filter_meta": {
            "confidence": "MEDIUM",
            "activated_detectors": ["generic_shell"],
            "raw_chars": 4000,
            "body_chars": 800,
            "body_tokens": 200,
        },
    }
    item = _to_result_item(row)
    # Legacy fields stay untouched (backwards compat with test_to_result_item_normalizes_fields).
    assert item["run_id"] == "abc-123"
    # New field carries through the projection.
    assert item["filter_meta"]["confidence"] == "MEDIUM"
    assert item["filter_meta"]["activated_detectors"] == ["generic_shell"]


def test_to_result_item_emits_empty_filter_meta_for_legacy_rows():
    """Pre-rewrite rows lack the column — must not crash, must not lie."""
    row = {
        "id": "legacy-1",
        "job_full_name": "team/old",
        "build_number": 1,
        "stage_name": "",
        "fingerprint": "",
        "filtered_logs": "",
        "analysis": "",
        "suggested_fix": "",
        "recommendation": "",
        "feedback_status": "",
        "created_at": "2026-01-01T00:00:00Z",
        # no filter_meta key at all
    }
    item = _to_result_item(row)
    assert item["filter_meta"] == {}
