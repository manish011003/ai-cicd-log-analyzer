"""Tests for GenericStackDetector + its bundled language instances.

Covers three layers:

1. **The detector contract** — ``node_stack`` and ``go_panic`` must obey
   the same :class:`Detector` protocol the orchestrator expects (probe,
   contribute, name, priority, no exceptions out of contribute).
2. **Language-specific correctness** — Node picks the topmost
   non-``node_modules`` frame; Go recovers the function name from the
   line above the file:line frame and skips runtime/GOROOT frames.
3. **Parameterised infrastructure** — a custom
   :class:`StackLanguage` plugged into :class:`GenericStackDetector`
   behaves identically to a registered bundled detector. This is the
   contract Layer 2 (YAML loader) will rely on.

All tests are offline (no Postgres, no Elasticsearch, no LLM, no network).
"""

from __future__ import annotations

import os

os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")
os.environ.setdefault("WORKER_API_KEY", "test")

from failure_analyzer_worker.filtering import (  # noqa: E402
    FilterConfig,
    UniversalFilter,
)
from failure_analyzer_worker.filtering.detectors import load_detectors  # noqa: E402
from failure_analyzer_worker.filtering.detectors.generic_stack import (  # noqa: E402
    GenericStackDetector,
    StackLanguage,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _filter(*, detectors: str = "auto", token_budget: int = 1500) -> UniversalFilter:
    cfg = FilterConfig(log_body_max_tokens=token_budget)
    return UniversalFilter(cfg, detectors=load_detectors(detectors))


# ── Node.js stack traces ──────────────────────────────────────────────────


NODE_STACK_LOG = """\
[INFO] Running e2e suite
> next build
TypeError: Cannot read property 'id' of undefined
    at Object.handler (/app/src/handlers/payment.js:42:15)
    at async Promise.all (index 0)
    at /app/src/server.js:128:5
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
    at next (/app/node_modules/express/lib/router/route.js:137:13)
    at Route.dispatch (/app/node_modules/express/lib/router/route.js:112:3)
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
    at /app/node_modules/express/lib/router/index.js:281:22
    at processTicksAndRejections (node:internal/process/task_queues:96:5)
[ERROR] suite failed
script returned exit code 1
"""


def test_node_stack_locates_topmost_project_frame_and_yields_high_confidence():
    out = _filter(detectors="node_stack").filter(NODE_STACK_LOG)

    assert out.primary_location is not None
    assert out.primary_location.kind == "source"
    # Topmost non-node_modules / non-node:internal frame is payment.js:42.
    assert out.primary_location.file.endswith("payment.js"), (
        f"expected payment.js, got {out.primary_location.file!r}"
    )
    assert out.primary_location.line == 42
    assert out.primary_location.column == 15
    assert "handler" in out.primary_location.function
    # TypeError is the cause class — it shows up in the message.
    assert "TypeError" in (out.primary_location.message or "")
    assert out.confidence == "HIGH"


def test_node_stack_skips_node_modules_and_node_internal_frames():
    out = _filter(detectors="node_stack").filter(NODE_STACK_LOG)
    # The chosen primary must NOT be an express or node:internal frame.
    primary_file = out.primary_location.file if out.primary_location else ""
    assert "node_modules" not in primary_file
    assert "node:internal" not in primary_file
    # But the locator's ranked list should still surface multiple project
    # candidates (payment.js, server.js) — second project frame must exist.
    project_locs = [
        loc
        for loc in out.locations
        if loc.kind == "source" and "node_modules" not in loc.file
    ]
    assert len(project_locs) >= 2
    files = {loc.file for loc in project_locs}
    assert any(f.endswith("payment.js") for f in files)
    assert any(f.endswith("server.js") for f in files)


def test_node_stack_falls_back_when_only_node_modules_frames_present():
    framework_only = """\
TypeError: oops
    at Layer.handle [as handle_request] (/app/node_modules/express/lib/router/layer.js:95:5)
    at next (/app/node_modules/express/lib/router/route.js:137:13)
    at processTicksAndRejections (node:internal/process/task_queues:96:5)
"""
    out = _filter(detectors="node_stack").filter(framework_only)
    assert out.primary_location is not None
    # The framework-only fallback caps confidence below the HIGH gate.
    assert out.primary_location.confidence < 0.6
    assert out.confidence in ("MEDIUM", "LOW")


# ── Go runtime panics ─────────────────────────────────────────────────────


GO_PANIC_LOG = """\
[INFO] Running go test
panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation code=0x1 addr=0x0 pc=0x4a86d0]

goroutine 1 [running]:
main.processPayment(0x0)
        /app/internal/payment/handler.go:42 +0xa5
main.handleRequest(0xc0000a8000)
        /app/internal/http/server.go:128 +0x123
main.main()
        /app/main.go:18 +0x4c
runtime.goexit()
        /usr/local/go/src/runtime/asm_amd64.s:1571 +0x1
[ERROR] FAIL    example.com/app 0.005s
exit status 2
"""


def test_go_panic_locates_topmost_project_frame_and_recovers_function_name():
    out = _filter(detectors="go_panic").filter(GO_PANIC_LOG)

    assert out.primary_location is not None
    assert out.primary_location.kind == "source"
    # Topmost project frame is handler.go:42 — runtime/asm_amd64.s is
    # framework code under GOROOT.
    assert out.primary_location.file.endswith("handler.go"), (
        f"expected handler.go, got {out.primary_location.file!r}"
    )
    assert out.primary_location.line == 42
    # Function name comes from the line ABOVE the file:line frame.
    assert "processPayment" in out.primary_location.function
    # "panic" is captured as the synthetic cause class.
    assert "panic" in (out.primary_location.message or "").lower()
    assert out.confidence == "HIGH"


def test_go_panic_skips_goroot_and_runtime_frames():
    out = _filter(detectors="go_panic").filter(GO_PANIC_LOG)
    primary_file = out.primary_location.file if out.primary_location else ""
    # Must not point at GOROOT or runtime internals.
    assert "/usr/local/go/" not in primary_file
    assert "runtime/asm_" not in primary_file


# ── Multi-detector activation ─────────────────────────────────────────────


def test_auto_loads_all_five_bundled_detectors():
    """``FILTER_DETECTORS=auto`` must surface every bundled detector,
    including the two new generic-stack ones, so the Settings page can
    show them as opt-out-able."""
    loaded = {d.name for d in load_detectors("auto")}
    assert {
        "java_stack",
        "python_traceback",
        "node_stack",
        "go_panic",
        "generic_shell",
    } <= loaded


def test_describe_filter_lists_new_detectors_with_their_own_descriptions():
    """Multiple :class:`GenericStackDetector` instances share one class,
    so the Settings page MUST pull the description from the instance
    attribute (not the shared class docstring) — otherwise Node and Go
    would render the same one-liner."""
    from failure_analyzer_worker.filtering import describe_filter

    cfg = FilterConfig(log_body_max_tokens=500)
    flt = UniversalFilter(cfg, detectors=load_detectors("auto"))

    class _StubSettings:
        log_body_max_tokens = 500
        log_body_max_chars = 4000
        filter_detectors = "auto"
        filter_max_active_detectors = 5

    snapshot = describe_filter(_StubSettings(), flt)
    by_name = {d["name"]: d for d in snapshot["detectors"]["available"]}

    assert "node_stack" in by_name
    assert "go_panic" in by_name
    assert by_name["node_stack"]["description"] != by_name["go_panic"]["description"]
    assert "Node" in by_name["node_stack"]["description"]
    assert "Go" in by_name["go_panic"]["description"]


def test_node_and_java_can_corroborate_when_log_carries_both():
    """A polyglot monorepo build that prints a Java stack *and* a Node
    stack must let both detectors fire — the locator decides the winner
    by ``(framework_penalty, kind_rank, confidence)``."""
    combined = """\
ERROR Build failed
\tat com.acme.svc.Wrapper.run(Wrapper.java:10)
TypeError: cannot read property 'x'
    at handler (/repo/app.js:7:3)
"""
    out = _filter(detectors="auto").filter(combined)
    activated = out.metadata["activated_detectors"]
    assert "java_stack" in activated
    assert "node_stack" in activated
    # At least one location from each detector survives in the ranked list.
    detectors_seen = {loc.detector for loc in out.locations}
    # ``detector`` on a corroborated location is a comma-joined list, so
    # check substring membership.
    assert any("java_stack" in d for d in detectors_seen)
    assert any("node_stack" in d for d in detectors_seen)


# ── StackLanguage as Layer-2 dress rehearsal ──────────────────────────────


def test_custom_stack_language_round_trips_through_generic_detector():
    """Constructing a brand-new detector from a :class:`StackLanguage`
    must work end-to-end without registering it — proving the data path
    Layer 2's YAML loader will eventually walk. Uses a synthetic
    "Ruby-ish" frame shape to avoid colliding with any bundled detector."""
    ruby_log = """\
NoMethodError: undefined method `charge` for nil:NilClass
    from /repo/app/services/payment.rb:42:in `process'
    from /repo/app/controllers/orders_controller.rb:17:in `create'
    from /usr/lib/ruby/3.2.0/gems/rails/lib/action_controller.rb:200:in `dispatch'
[ERROR] tests failed
"""
    lang = StackLanguage(
        name="ruby_stack_test",
        priority=50,
        description="Test-only Ruby detector (not registered in _BUNDLED).",
        probe_pattern=r"^\s+from\s+\S+\.rb:\d+",
        frame_pattern=(
            r"^\s+from\s+(?P<file>\S+\.rb):(?P<line>\d+)"
            r"(?::in\s+`(?P<function>[^']+)')?"
        ),
        framework_paths=("/usr/lib/ruby/", "/gems/"),
        bug_site="top",
        cause_pattern=(
            r"^(?P<cls>(?:[\w:]+\.)*[A-Z]\w*(?:Error|Exception))"
            r"(?::\s*(?P<msg>.*))?$"
        ),
        cause_position="above",
    )
    det = GenericStackDetector(lang)

    cfg = FilterConfig(log_body_max_tokens=500)
    flt = UniversalFilter(cfg, detectors=[det])
    out = flt.filter(ruby_log, stage_name="rspec")

    assert out.primary_location is not None
    assert out.primary_location.kind == "source"
    assert out.primary_location.file.endswith("payment.rb")
    assert out.primary_location.line == 42
    assert "process" in out.primary_location.function
    assert "NoMethodError" in (out.primary_location.message or "")
    # Detector name flows through to the activated list / Contribution.
    assert "ruby_stack_test" in out.metadata["activated_detectors"]
