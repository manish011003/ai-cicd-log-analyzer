"""Universal log filter — structural, baseline-aware, plug-and-play.

Public surface for the rest of the worker:

* :class:`Filter` (protocol) + :func:`create_filter` — the modern entry
  point. Returns a :class:`FilterResult` with the LLM-facing ``body``
  *and* structured ``primary_location`` + observability metadata.
* :class:`FailureLocation`, :class:`FilterResult`, :class:`Line`,
  :class:`Severity`, :class:`Kind`, :class:`Contribution`,
  :class:`Detector` — the value objects every protocol implementer needs.

Legacy surface preserved for back-compat (``log_processor.py`` shim,
``tests/test_log_processor.py``, ``try_filter.py``):

* :func:`filter_logs` — plain-string body, no citation prefixes.
* :func:`generate_fingerprint`, :func:`primary_error` — unchanged.
* :class:`LogProcessor` — wraps a filtered body with the legacy
  ``[METADATA]`` header. Internally re-implemented on top of the new
  pipeline so the structural collapsers + confidence flag flow through
  to old callers automatically.

This module follows the layout convention set by ``llm/`` and
``vectorstore/``: only protocols and the factory are re-exported here;
the concrete passes live in their own files.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import (
    Confidence,
    Contribution,
    Detector,
    FailureLocation,
    Filter,
    FilterResult,
    Kind,
    Line,
    LocationKind,
    Severity,
)
from .factory import create_filter, describe_filter
from .orchestrator import FilterConfig, UniversalFilter
from .render import (
    exit_code as _exit_code,
    generate_fingerprint,
    primary_error,
)

if TYPE_CHECKING:
    from ..config import WorkerSettings


__all__ = [
    # Modern API
    "Confidence",
    "Contribution",
    "Detector",
    "FailureLocation",
    "Filter",
    "FilterConfig",
    "FilterResult",
    "Kind",
    "Line",
    "LocationKind",
    "Severity",
    "UniversalFilter",
    "create_filter",
    "describe_filter",
    # Legacy API (kept import-compatible)
    "LogProcessor",
    "filter_logs",
    "generate_fingerprint",
    "primary_error",
]


# ── Legacy adapters ───────────────────────────────────────────────────────
#
# These three functions keep the pre-refactor public surface alive so
# ``log_processor.py`` (the back-compat shim used by ``try_worker.py`` and
# external scripts) and the existing pytest gate keep working. They route
# through the new pipeline so the structural wins apply automatically —
# only the *string format* matches the legacy expectations.


def _default_settings():
    """Lazy-load the module-level :class:`WorkerSettings` once.

    Kept lazy so importing ``filtering`` never triggers the pydantic-settings
    read (which would force the worker's ``.env`` to be present even in
    pure-Python tests that only exercise the filter).
    """
    from ..config import settings  # local import to keep import-time light

    return settings


_DEFAULT_FILTER: Filter | None = None


def _get_default_filter() -> Filter:
    global _DEFAULT_FILTER
    if _DEFAULT_FILTER is None:
        _DEFAULT_FILTER = create_filter(_default_settings())
    return _DEFAULT_FILTER


def reset_default_filter() -> None:
    """Drop the cached default filter (tests + post-config reloads)."""
    global _DEFAULT_FILTER
    _DEFAULT_FILTER = None


def filter_logs(raw_text: str) -> str:
    """Legacy entry point: return only the structurally-collapsed body string.

    No ``[METADATA]`` header (the legacy contract: ``filter_logs`` returns
    the body alone; :class:`LogProcessor` adds the header). No citation
    prefixes either — those are reserved for callers that go through the
    modern :class:`Filter` protocol.

    Use :func:`create_filter` (or :func:`failure_analyzer_worker.filtering.create_filter`)
    in new code; this function exists only to keep
    ``test_log_processor.py`` and ``try_filter.py`` import-compatible.
    """
    if not raw_text:
        return ""

    # Build a one-off filter with the same config but no detectors — the
    # legacy contract didn't include detector-driven boosts; staying close
    # to the old behavior keeps the test golden strings predictable.
    from .baseline import SelfSketch
    from . import anchors as _anchors
    from . import render as _render
    from . import structural as _structural
    from . import tokenize as _tokenize

    settings = _default_settings()
    lines = _tokenize.tokenize(raw_text)
    if not lines:
        return ""
    _structural.collapse(lines)
    _ = _exit_code  # silence unused-import lints
    # Pass 3 — self-baseline only (no ES, no provider).
    from .baseline import apply as _baseline_apply

    _baseline_apply(lines, SelfSketch(lines))
    _anchors.score(lines)
    token_budget = int(getattr(settings, "log_body_max_tokens", 1500))
    selected = _anchors.select(lines, token_budget=token_budget)
    body = _render.render_plain_body(lines, selected)

    # Hard ceiling fallback (paranoia against budget overshoot).
    cap = int(getattr(settings, "log_body_max_chars", 8000))
    if len(body) > cap:
        body = body[:cap] + "\n... [hard ceiling truncated]"
    return body


class LogProcessor:
    """Legacy adapter — wraps filtered body with the ``[METADATA]`` header.

    Routes through :func:`create_filter` so the new structural pipeline,
    confidence flag, and Location resolution all run, but the *string
    shape* is exactly what existing tests assert on (``[METADATA]``,
    ``Primary Error:``, ``Exit Code:`` substrings remain present).
    """

    def __init__(self, filter_: Filter | None = None) -> None:
        self._filter = filter_

    def _resolve_filter(self) -> Filter:
        if self._filter is not None:
            return self._filter
        return _get_default_filter()

    def format_with_metadata(self, raw: str, body: str) -> str:
        # If a caller pre-computed ``body`` via ``filter_logs(raw)`` and
        # hands it back here (the legacy flow), reuse it instead of
        # re-filtering. We still emit the new richer header.
        from . import render as _render_mod

        primary = _render_mod.primary_error(body)
        code = _render_mod.exit_code(raw + "\n" + body)
        raw_chars = len(raw)
        body_chars = len(body)
        body_tokens = max(1, (body_chars + 3) // 4)
        header = _render_mod.render_metadata_header(
            raw_chars=raw_chars,
            body_chars=body_chars,
            body_tokens=body_tokens,
            primary=primary,
            code=code,
            confidence="MEDIUM",  # no structural confidence info on this path
            primary_location=None,
            detectors=(),
            baseline_version="legacy",
            collapse_stats={},
        )
        return header + body + "\n---"

    def process(self, raw: str) -> str:
        result = self._resolve_filter().filter(raw)
        # ``result.body`` already includes the [METADATA] header on this path.
        return result.body
