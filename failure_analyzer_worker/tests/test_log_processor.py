"""Smoke tests for the minimal log filter + fingerprint + LogProcessor.

Run from the repo root::

    pytest failure_analyzer_worker/tests -q

These tests deliberately avoid Elasticsearch and the LLM (no network calls),
so they double as a CI gate for the pure-Python pipeline.
"""

from __future__ import annotations

import os

# Sane defaults so importing the package doesn't require a real ``.env``.
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")
os.environ.setdefault("WORKER_API_KEY", "test")

from failure_analyzer_worker import log_processor  # noqa: E402
from failure_analyzer_worker.log_processor import (  # noqa: E402
    LogProcessor,
    filter_logs,
    generate_fingerprint,
)


SAMPLE_LOG = """\
[Pipeline] { (Build)
[INFO] Scanning for projects...
[INFO] Building demo 1.0
[INFO] Downloading from central: https://repo.maven.apache.org/foo.jar
[INFO] Downloading from central: https://repo.maven.apache.org/bar.jar
[INFO] Downloading from central: https://repo.maven.apache.org/baz.jar
2026-04-15T10:00:00Z INFO   noisy line
2026-04-15T10:00:00Z INFO   noisy line
2026-04-15T10:00:00Z INFO   noisy line
2026-04-15T10:00:00Z INFO   noisy line
ERROR Application failed to start
Caused by: java.net.ConnectException: Connection refused
\tat com.example.Client.call(Client.java:42)
\tat com.example.Runner.main(Runner.java:10)
[INFO] BUILD FAILURE
"""


def test_filter_logs_keeps_smoking_gun_drops_progress_noise():
    out = filter_logs(SAMPLE_LOG)
    assert "Connection refused" in out, "primary error must survive filtering"
    assert "ConnectException" in out
    # Pipeline / Maven progress noise must be removed.
    assert "Downloading from central" not in out
    assert "[Pipeline]" not in out
    assert "Scanning for projects" not in out


def test_filter_logs_strips_iso_timestamps_at_line_start():
    raw = "2026-04-15T10:00:00Z ERROR boom\n"
    out = filter_logs(raw)
    assert out.startswith("ERROR boom")
    assert "2026-04-15" not in out


def test_filter_logs_normalizes_crlf_and_bom():
    raw = "\ufefffirst line\r\nsecond line\rthird line\n"
    out = filter_logs(raw)
    assert "first line" in out
    assert "second line" in out
    assert "third line" in out
    assert "\r" not in out
    assert "\ufeff" not in out


def test_filter_logs_collapses_consecutive_duplicates_with_marker():
    repeated = "\n".join(["WARN msg"] * 12) + "\nERROR boom\n"
    out = filter_logs(repeated)
    # Only one copy survives, with a "(repeated)" marker, and ERROR is preserved.
    assert out.count("WARN msg") == 1
    assert "(repeated)" in out
    assert "ERROR boom" in out


def test_filter_logs_respects_body_max_chars_cap():
    huge = ("ERROR line %d\n" % 0) + "\n".join(
        f"INFO filler line {i}" for i in range(20000)
    ) + "\nERROR final boom\n"
    out = filter_logs(huge)
    # Cap honoured (with a small slack for the truncation marker).
    assert len(out) <= log_processor.settings.log_body_max_chars + 64
    # Tail (where failures live) is preserved.
    assert "ERROR final boom" in out


def test_generate_fingerprint_focuses_on_root_cause_and_exception():
    fp = generate_fingerprint(filter_logs(SAMPLE_LOG), stage_name="Build")
    assert "stage: Build" in fp
    # ConnectException is captured as an exception type.
    assert "ConnectException" in fp
    # Caused-by payload (root cause) is included.
    assert "root_cause:" in fp
    # Generic noise phrases must NOT appear -- they would dilute embeddings.
    assert "not found" not in fp.lower()
    assert "timed out" not in fp.lower()


def test_log_processor_metadata_block_is_present_and_picks_real_primary_error():
    text = LogProcessor().process(SAMPLE_LOG)
    assert "[METADATA]" in text
    assert "Primary Error:" in text
    assert "Exit Code:" in text
    # Primary error should be the real cause, not the Maven "BUILD FAILURE" marker.
    primary_line = next(ln for ln in text.splitlines() if ln.startswith("- Primary Error:"))
    assert "BUILD FAILURE" not in primary_line
    assert "Connect" in primary_line  # ConnectException / Connection refused both qualify


def test_filter_logs_returns_empty_for_empty_input():
    assert filter_logs("") == ""
    assert filter_logs("   \n\n  \r\n") == ""


def test_reset_es_client_does_not_require_running_es():
    # Just make sure the helper is callable without raising.
    log_processor.reset_es_client()
