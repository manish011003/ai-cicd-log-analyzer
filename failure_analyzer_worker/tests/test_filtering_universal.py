"""Tests for the universal four-pass filter + detector plug-in surface.

These exercise the *modern* :class:`Filter` API (returns a structured
:class:`FilterResult` with a primary location, confidence flag, and per-pass
observability). The legacy ``filter_logs`` / ``LogProcessor`` surface is
covered by ``test_log_processor.py``.

Goals these tests pin (so they cannot silently regress):

* **Goal 1 — Universal:** the four passes work on a stack with zero
  detectors enabled (``FILTER_DETECTORS=none``).
* **Goal 2 — Accurate location:** ``generic_shell`` produces a structured
  ``FailureLocation`` for any log ending in a non-zero exit.
* **Goal 3 — Token reduction:** filtering a 50× repeat-storm reduces body
  bytes by at least 5× and the smoking-gun error survives.
"""

from __future__ import annotations

import os

os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")
os.environ.setdefault("WORKER_API_KEY", "test")

from failure_analyzer_worker.config import settings  # noqa: E402
from failure_analyzer_worker.filtering import (  # noqa: E402
    FailureLocation,
    FilterConfig,
    FilterResult,
    UniversalFilter,
    create_filter,
)
from failure_analyzer_worker.filtering.detectors import (  # noqa: E402
    load_detectors,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _filter(*, detectors: str = "auto", token_budget: int = 1500) -> UniversalFilter:
    """Build a fresh :class:`UniversalFilter` for one test, no shared state."""
    cfg = FilterConfig(log_body_max_tokens=token_budget)
    return UniversalFilter(cfg, detectors=load_detectors(detectors))


JAVA_STACK_LOG = """\
[INFO] Building demo 1.0
[INFO] Compiling 12 source files
2026-04-15T10:00:00Z ERROR Application failed to start
Caused by: java.net.ConnectException: Connection refused
\tat java.base/sun.nio.ch.Net.pollConnect(Native Method)
\tat java.base/sun.nio.ch.Net.pollConnectNow(Net.java:672)
\tat java.base/sun.nio.ch.NioSocketImpl.timedFinishConnect(NioSocketImpl.java:546)
\tat java.base/java.net.SocksSocketImpl.connect(SocksSocketImpl.java:327)
\tat java.base/java.net.Socket.connect(Socket.java:633)
\tat org.apache.http.impl.conn.DefaultHttpClientConnectionOperator.connect(DefaultHttpClientConnectionOperator.java:142)
\tat com.acme.billing.PaymentClient.charge(PaymentClient.java:84)
\tat com.acme.billing.OrderService.checkout(OrderService.java:201)
\tat com.acme.billing.OrderController.handle(OrderController.java:55)
[INFO] BUILD FAILURE
"""


# ── Goal 1: universal core works without detectors ────────────────────────


def test_universal_core_works_with_no_detectors():
    f = _filter(detectors="none")
    out = f.filter(JAVA_STACK_LOG, stage_name="Build", job_name="demo")

    assert isinstance(out, FilterResult)
    assert "ConnectException" in out.body, "the smoking-gun line must survive"
    assert "Connection refused" in out.body
    # Pass 2 collapsed the long framework stack.
    assert "frame(s) elided" in out.body or "frames elided" in out.body
    # The L<idx>: citation prefix is on every kept line in the modern body.
    assert "\nL" in out.body or out.body.startswith("---")


def test_universal_core_strips_progress_and_banners():
    log = "\n".join(
        ["====================================="]
        + [f"1f7be4cba: Downloading [={i}>] {i}MB/45MB" for i in range(10)]
        + ["1f7be4cba: Pull complete"]
        + ["ERROR final boom"]
    )
    out = _filter(detectors="none").filter(log)
    assert "ERROR final boom" in out.body
    # Banner row is dropped …
    assert "=========" not in out.body
    # … and the 10-line progress burst is collapsed.
    assert "Downloading" not in out.body or "<progress:" in out.body


# ── Goal 2: structured FailureLocation from generic_shell ─────────────────


SHELL_FAILURE_LOG = """\
+ npm run build
> next build
> Running 23 tests
> 22 passed, 1 failed
script returned exit code 1
"""


def test_generic_shell_locates_command_and_exit_code():
    out = _filter(detectors="generic_shell").filter(SHELL_FAILURE_LOG)

    assert out.primary_location is not None
    assert out.primary_location.kind == "command"
    assert "npm" in (out.primary_location.command or "")
    assert "1" in out.primary_location.message  # "exited with code 1"
    assert out.confidence in ("HIGH", "MEDIUM")  # has an exit anchor
    # The locator field also surfaces in the metadata header.
    assert "Location:" in out.body
    assert "Confidence:" in out.body


def test_locator_falls_back_to_log_kind_without_detectors():
    """Universal core alone (no detectors) still produces a localized hint."""
    out = _filter(detectors="none").filter(JAVA_STACK_LOG)
    # Either log-line fallback or an embedded core suggestion — never empty.
    assert out.primary_location is not None
    assert out.primary_location.kind in ("log", "source", "test", "command")
    assert out.primary_location.log_line_idx >= 0


# ── Goal 3: token reduction (5×+ on repeat storms) ────────────────────────


def test_repeat_storm_compresses_at_least_five_fold():
    storm = (
        "INFO setup\n" * 5
        + "\n".join([f"WARN endpoint=http://service-{i}/health 503" for i in range(60)])
        + "\nERROR final boom\n"
    )
    out = _filter(detectors="none").filter(storm)
    raw_chars = len(storm)
    compression = raw_chars / max(1, out.body_chars)
    assert compression >= 5.0, f"expected >=5× compression, got {compression:.1f}×"
    assert "ERROR final boom" in out.body
    # The WARN repeat got annotated, not erased.
    assert "repeated" in out.body or "elided" in out.body


def test_severity_at_error_is_never_baseline_dropped():
    """Even when the baseline says 'common', ERROR-tier lines must survive."""
    log = "\n".join(["INFO baseline noise"] * 20 + ["ERROR rare actual problem"])
    out = _filter(detectors="none").filter(log)
    assert "ERROR rare actual problem" in out.body


# ── Confidence routing & LOW fallback ─────────────────────────────────────


def test_low_confidence_when_no_severity_anchors_present():
    """All-INFO logs trigger the insufficient-signal fallback."""
    log = "\n".join([f"INFO step {i}" for i in range(40)])
    out = _filter(detectors="none").filter(log)
    assert out.confidence in ("LOW", "MEDIUM")  # never HIGH on pure noise
    if out.confidence == "LOW":
        assert "LOW CONFIDENCE" in out.body


# ── Detector plug-in contract ─────────────────────────────────────────────


def test_detector_load_selection_respects_settings_strings():
    assert load_detectors("none") == []
    assert any(d.name == "generic_shell" for d in load_detectors("auto"))
    assert all(d.name == "generic_shell" for d in load_detectors("generic_shell"))
    # Unknown detector names are silently skipped (CI never breaks on a typo).
    assert load_detectors("definitely_not_a_real_detector") == []


def test_filter_result_metadata_contains_observability_payload():
    out = _filter(detectors="auto").filter(SHELL_FAILURE_LOG, stage_name="Test")
    meta = out.metadata
    assert "confidence" in meta
    assert "activated_detectors" in meta
    assert "baseline_version" in meta
    assert "collapse_stats" in meta
    assert "primary_location" in meta
    assert meta["raw_chars"] > 0
    assert meta["body_tokens"] >= 1


# ── Factory wiring matches the existing repo convention ──────────────────


def test_create_filter_returns_a_universal_filter_from_settings():
    f = create_filter(settings)
    assert isinstance(f, UniversalFilter)
    out = f.filter("ERROR boom\nexit 1", stage_name="x", job_name="y")
    assert isinstance(out, FilterResult)
    assert "boom" in out.body


# ── java_stack detector ───────────────────────────────────────────────────


def test_java_stack_locates_topmost_project_frame_and_yields_high_confidence():
    out = _filter(detectors="java_stack").filter(JAVA_STACK_LOG)

    assert out.primary_location is not None
    assert out.primary_location.kind == "source"
    # PaymentClient is the topmost project frame after the java.base
    # internals — that's the bug site for a Java stack.
    assert "PaymentClient.java" in out.primary_location.file
    assert out.primary_location.line == 84
    assert "charge" in out.primary_location.function
    # ConnectException is the cause class; it shows up in the message.
    assert "ConnectException" in (out.primary_location.message or "")
    assert out.confidence == "HIGH"


def test_java_stack_falls_back_when_only_framework_frames_present():
    framework_only = """\
ERROR Internal infrastructure failure
\tat java.base/sun.nio.ch.Net.pollConnect(Native Method)
\tat java.base/sun.nio.ch.Net.pollConnectNow(Net.java:672)
\tat java.base/java.net.Socket.connect(Socket.java:633)
"""
    out = _filter(detectors="java_stack").filter(framework_only)
    assert out.primary_location is not None
    # The framework-internal fallback caps confidence below the HIGH gate,
    # so we degrade gracefully instead of pointing at a JDK frame as "the bug".
    assert out.primary_location.confidence < 0.6
    assert out.confidence in ("MEDIUM", "LOW")


def test_java_stack_handles_caused_by_chain():
    chained = """\
ERROR Service failed
java.lang.RuntimeException: wrapper
\tat com.acme.svc.Wrapper.run(Wrapper.java:10)
Caused by: java.io.IOException: actual
\tat com.acme.svc.Inner.doIt(Inner.java:55)
"""
    out = _filter(detectors="java_stack").filter(chained)
    # The locator may pick either project frame, but it must be in the
    # project (com.acme.svc), not framework code.
    assert out.primary_location is not None
    assert "com.acme.svc" in out.primary_location.function
    assert out.primary_location.kind == "source"
    # Both project frames are kept marked so neither disappears.
    assert "Wrapper.java" in out.body
    assert "Inner.java" in out.body


# ── python_traceback detector ─────────────────────────────────────────────


PYTHON_TRACEBACK_LOG = """\
[INFO] Running tests
Traceback (most recent call last):
  File "/repo/src/app/main.py", line 42, in main
    return handler(request)
  File "/repo/src/app/handler.py", line 17, in handler
    return user_svc.get(uid)
  File "/usr/lib/python3.11/site-packages/sqlalchemy/orm/query.py", line 290, in get
    raise NoResultFound()
sqlalchemy.exc.NoResultFound: No row was found
[INFO] tests failed
"""


def test_python_traceback_locates_bottom_most_project_frame():
    out = _filter(detectors="python_traceback").filter(PYTHON_TRACEBACK_LOG)

    assert out.primary_location is not None
    assert out.primary_location.kind == "source"
    # Bottom-most non-site-packages frame is /repo/src/app/handler.py:17.
    # That's the deepest project call before the exception was raised.
    assert out.primary_location.file.endswith("handler.py")
    assert out.primary_location.line == 17
    assert "handler" in out.primary_location.function
    assert "NoResultFound" in (out.primary_location.message or "")
    assert out.confidence == "HIGH"


def test_python_traceback_handles_chained_exceptions():
    chained = """\
Traceback (most recent call last):
  File "/repo/src/svc/a.py", line 5, in foo
    bar()
ValueError: first

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/repo/src/svc/b.py", line 11, in baz
    raise RuntimeError("second")
RuntimeError: second
"""
    out = _filter(detectors="python_traceback").filter(chained)
    # Two locations expected — one per sub-traceback.
    source_locs = [loc for loc in out.locations if loc.kind == "source"]
    assert len(source_locs) >= 2
    files = {loc.file for loc in source_locs}
    assert any(f.endswith("a.py") for f in files)
    assert any(f.endswith("b.py") for f in files)


def test_python_traceback_skips_site_packages_for_primary_pick():
    """A project frame, even higher up the stack, must outrank a deeper third-party frame."""
    out = _filter(detectors="python_traceback").filter(PYTHON_TRACEBACK_LOG)
    assert out.primary_location is not None
    assert "site-packages" not in out.primary_location.file


# ── Multi-detector corroboration ──────────────────────────────────────────


def test_auto_loads_three_detectors_and_picks_source_over_command():
    """With auto-detectors, a Java stack failure prefers the source frame
    over the generic_shell command location even if a non-zero exit
    appears later in the log."""
    log = JAVA_STACK_LOG + "\n+ mvn verify\nscript returned exit code 1\n"
    out = _filter(detectors="auto").filter(log)

    assert out.primary_location is not None
    assert out.primary_location.kind == "source"  # source > command in locator ranking
    assert "PaymentClient" in out.primary_location.function
    activated = out.metadata["activated_detectors"]
    assert "java_stack" in activated
    assert "generic_shell" in activated


# ── describe_filter (powers /filter-config + Settings page) ───────────────


def test_describe_filter_reports_active_and_bundled_detectors():
    """The introspection helper drives the UI Settings page — pin its shape."""
    from failure_analyzer_worker.filtering import describe_filter

    flt = create_filter(settings)
    snapshot = describe_filter(settings, flt)

    # Top-level shape is stable and JSON-safe (no dataclasses, no sets).
    assert set(snapshot.keys()) >= {"settings", "detectors", "implementation"}
    knobs = snapshot["settings"]
    for key in (
        "log_body_max_tokens",
        "log_body_max_chars",
        "filter_detectors",
        "filter_max_active_detectors",
        "use_self_baseline",
    ):
        assert key in knobs, f"missing knob {key!r}"
    assert isinstance(knobs["log_body_max_tokens"], int)
    assert knobs["log_body_max_tokens"] > 0

    active = snapshot["detectors"]["active"]
    available = snapshot["detectors"]["available"]

    # Bundled list is the canonical inventory — must include all three.
    available_names = {d["name"] for d in available}
    assert {"java_stack", "python_traceback", "generic_shell"} <= available_names

    # Every active detector also appears in the bundled list with active=True.
    for d in active:
        assert d["name"] in available_names
        assert d["active"] is True
        assert isinstance(d["priority"], int)
        assert d["description"], "detector description must not be empty"


def test_describe_filter_marks_disabled_detectors_inactive():
    """An allowlist that excludes a bundled detector must show it as inactive."""
    from failure_analyzer_worker.filtering import describe_filter

    cfg = FilterConfig(log_body_max_tokens=500)
    flt = UniversalFilter(cfg, detectors=load_detectors("generic_shell"))

    # Build a stand-in settings object that exposes the same shape as
    # WorkerSettings for describe_filter — no need to mutate real settings.
    class _StubSettings:
        log_body_max_tokens = 500
        log_body_max_chars = 4000
        filter_detectors = "generic_shell"
        filter_max_active_detectors = 5

    snapshot = describe_filter(_StubSettings(), flt)

    by_name = {d["name"]: d for d in snapshot["detectors"]["available"]}
    assert by_name["generic_shell"]["active"] is True
    # java_stack and python_traceback are bundled but excluded from the live filter.
    assert by_name["java_stack"]["active"] is False
    assert by_name["python_traceback"]["active"] is False
