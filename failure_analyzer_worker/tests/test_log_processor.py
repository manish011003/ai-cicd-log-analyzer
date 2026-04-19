"""Smoke tests for the log filtering / fingerprinting pipeline.

Run from the repo root:

    pytest failure_analyzer_worker/tests -q

These tests deliberately avoid Elasticsearch and the LLM (no network calls),
so they double as a CI gate for the pure-Python pipeline.
"""

from __future__ import annotations

import os

# Make sure the package picks up sane defaults even when run without a .env.
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")
os.environ.setdefault("WORKER_API_KEY", "test")

from failure_analyzer_worker import log_processor  # noqa: E402
from failure_analyzer_worker.log_processor import (  # noqa: E402
    LogProcessor,
    filter_logs,
    generate_fingerprint,
    normalize_log_text,
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


def test_normalize_log_text_strips_bom_and_normalizes_newlines():
    raw = "\ufeffline-1\r\nline-2\rline-3\n"
    out = normalize_log_text(raw)
    assert out.startswith("line-1")
    assert out.count("\n") == 3


def test_filter_logs_keeps_smoking_gun_drops_progress_noise():
    out = filter_logs(SAMPLE_LOG)
    assert "Connection refused" in out, "primary error must survive filtering"
    assert "ConnectException" in out
    # Progress / pipeline noise must be removed.
    assert "Downloading from central" not in out
    assert "[Pipeline] {" not in out


def test_filter_logs_collapses_repeated_template_lines():
    repeated = "\n".join(["WARN msg"] * 12) + "\nERROR boom\n"
    out = filter_logs(repeated)
    # The collapse marker must appear, and ERROR is preserved.
    assert "Repeated" in out
    assert "ERROR boom" in out


def test_generate_fingerprint_includes_stage_and_signal():
    body = filter_logs(SAMPLE_LOG)
    fp = generate_fingerprint(body, stage_name="Build")
    assert fp.startswith("stage:Build")
    assert "ConnectException" in fp
    # Either root_cause or signals captured the connection-refused signal.
    assert "connection refused" in fp.lower() or "ConnectException" in fp


def test_log_processor_metadata_block_is_present():
    text = LogProcessor().process(SAMPLE_LOG)
    assert "[METADATA SUMMARY]" in text
    assert "Primary Error:" in text
    assert "Critical Exit Code:" in text


def test_reset_es_client_does_not_require_running_es():
    # Just make sure the helper is callable without raising.
    log_processor.reset_es_client()
