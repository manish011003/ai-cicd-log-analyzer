"""Composition root for the four-pass filter.

This is the only module that knows the order of operations:

    tokenize → structural → detector phase → baseline → anchors → render

Two non-obvious responsibilities live here:

* **Trust boundary for detectors.** Each detector runs under try/except
  with its own metric; a bug in one detector logs and is skipped, never
  takes down the run. Activation is capped at ``max_active`` per log so
  detector count cannot bloat latency.
* **Confidence routing.** When the anchor selector found no high-scoring
  cluster, the orchestrator emits the LOW-confidence body (raw tail with
  a banner) instead of pretending it found a root cause. This is the
  ``insufficient signal`` escape hatch — the only RAG-pipeline analog
  worth keeping verbatim.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from . import anchors as _anchors
from . import baseline as _baseline
from . import locator as _locator
from . import render as _render
from . import structural as _structural
from . import tokenize as _tokenize
from .base import (
    Confidence,
    Contribution,
    Detector,
    FailureLocation,
    FilterResult,
    Line,
    Severity,
)
from .baseline import BaselineSketch, EmptySketch, SelfSketch

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FilterConfig:
    """Tunables surfaced from :class:`WorkerSettings`.

    Kept as its own dataclass (not raw ``WorkerSettings``) so the filter
    package has no transitive import on pydantic-settings — keeps the
    pure-Python promise for unit tests.
    """

    log_body_max_tokens: int = 1500
    log_body_max_chars: int = 8000  # hard ceiling fallback
    max_active_detectors: int = 5
    use_self_baseline: bool = True  # PR 1: no Baseline A yet


class UniversalFilter:
    """Default :class:`~filtering.base.Filter` implementation.

    Holds the configured detectors and per-``(job, stage)`` baseline
    sketch provider. Stateless across calls — safe to share a single
    instance across worker threads.
    """

    def __init__(
        self,
        config: FilterConfig,
        detectors: Sequence[Detector] = (),
        sketch_provider=None,
    ) -> None:
        self._cfg = config
        # Sort detectors by priority descending so locator ties break the
        # right way and the activation cap keeps the highest-priority ones.
        self._detectors = sorted(detectors, key=lambda d: -getattr(d, "priority", 0))
        self._sketch_provider = sketch_provider  # callable: (job, stage) -> BaselineSketch | None

    # ── Introspection (used by /filter-config and the UI Settings page) ──

    @property
    def config(self) -> "FilterConfig":
        """Read-only view of the active :class:`FilterConfig`."""
        return self._cfg

    @property
    def detectors(self) -> tuple[Detector, ...]:
        """Active detectors, sorted by descending priority.

        Returned as a tuple so callers cannot mutate the run order. Tests
        and ``/filter-config`` use this to render the live wiring.
        """
        return tuple(self._detectors)

    # ── Filter protocol ──

    def filter(
        self,
        raw_logs: str,
        *,
        stage_name: str = "",
        job_name: str = "",
    ) -> FilterResult:
        raw_chars = len(raw_logs or "")

        # Pass 1 — tokenize. Never drops.
        lines = _tokenize.tokenize(raw_logs or "")
        if not lines:
            return _empty_result(raw_chars)

        # Pass 2 — structural collapse (banners, stacks, JSON, progress, repeats).
        _structural.collapse(lines)
        collapse_stats = _summarize_collapse(lines)

        # Detector phase — runs *after* structural collapse so detectors
        # see the same Line metadata the locator will, and *before* the
        # baseline so a detector's keep_marks survive baseline drops.
        probe = (raw_logs or "")[:4096]
        contribs = self._run_detectors(probe, lines)
        active_names = [c.detector for c in contribs]
        _merge_contributions(lines, contribs)

        # Pass 3 — baseline diff.
        sketch = self._resolve_sketch(job_name, stage_name, lines)
        baseline_stats = _baseline.apply(lines, sketch)

        # Pass 4 — score + budget-fill.
        _anchors.score(lines)
        selected = _anchors.select(lines, token_budget=self._cfg.log_body_max_tokens)

        # Locator — pick the failure location from all contributed candidates.
        all_locations: list[FailureLocation] = []
        for c in contribs:
            all_locations.extend(c.locations)
        primary, ranked = _locator.pick(all_locations, lines)

        # Confidence routing.
        confidence, low_reason = _confidence(lines, selected, primary)

        # Render the body.
        body_text = _render.render_body(lines, selected, with_citation=True)
        if confidence == "LOW":
            body_text = _render.render_low_confidence_banner(low_reason) + (
                body_text or _tail_excerpt(lines, self._cfg.log_body_max_chars // 2)
            )

        # Hard ceiling — defensive cap in case the budget overshot for some reason.
        if len(body_text) > self._cfg.log_body_max_chars:
            body_text = body_text[: self._cfg.log_body_max_chars] + "\n... [hard ceiling truncated]"

        # Prefer the detector's structured message when present — it
        # already carries the exception class plus the failure summary
        # (``ConnectException: Connection refused``,
        # ``sqlalchemy.exc.NoResultFound: No row was found``). Falls back
        # to the regex-based extractor for logs no detector localized.
        primary_err_text = (
            primary.message
            if primary is not None and primary.message
            else _render.primary_error(body_text)
        )
        exit_code_text = _render.exit_code((raw_logs or "") + "\n" + body_text)
        body_tokens = max(1, (len(body_text) + 3) // 4)

        header = _render.render_metadata_header(
            raw_chars=raw_chars,
            body_chars=len(body_text),
            body_tokens=body_tokens,
            primary=primary_err_text,
            code=exit_code_text,
            confidence=confidence,
            primary_location=primary,
            detectors=active_names,
            baseline_version=str(baseline_stats.get("baseline_version", "empty")),
            collapse_stats=collapse_stats,
        )

        wrapped = header + body_text + "\n---"
        fingerprint = _render.generate_fingerprint(body_text, stage_name)

        metadata: dict = {
            "confidence": confidence,
            "low_confidence_reason": low_reason,
            "activated_detectors": active_names,
            "baseline_version": baseline_stats.get("baseline_version"),
            "baseline_dropped": baseline_stats.get("baseline_dropped", 0),
            "baseline_novel": baseline_stats.get("baseline_novel", 0),
            "collapse_stats": collapse_stats,
            "raw_chars": raw_chars,
            "body_chars": len(body_text),
            "body_tokens": body_tokens,
            "selected_count": len(selected),
            "primary_location": _location_to_dict(primary),
            "locations": [_location_to_dict(loc) for loc in ranked],
            "stage_name": stage_name,
            "job_name": job_name,
        }

        return FilterResult(
            body=wrapped,
            fingerprint=fingerprint,
            primary_error=primary_err_text,
            primary_location=primary,
            locations=ranked,
            confidence=confidence,
            metadata=metadata,
            raw_chars=raw_chars,
            body_chars=len(wrapped),
            body_tokens=body_tokens,
        )

    # ── Internals ──

    def _run_detectors(self, probe: str, lines: list[Line]) -> list[Contribution]:
        out: list[Contribution] = []
        if not self._detectors:
            return out
        cap = max(1, int(self._cfg.max_active_detectors))
        for det in self._detectors:
            if len(out) >= cap:
                break
            try:
                if not det.activates_on(probe):
                    continue
                c = det.contribute(lines)
            except Exception:  # noqa: BLE001 — never let one detector break the run
                logger.exception(
                    "detector %r failed; skipping",
                    getattr(det, "name", det),
                )
                continue
            if c is None:
                continue
            # Self-validation: detector activated but had nothing to say.
            if not (c.score_deltas or c.drop_marks or c.keep_marks or c.locations):
                continue
            out.append(c)
        return out

    def _resolve_sketch(
        self, job: str, stage: str, lines: Sequence[Line]
    ) -> BaselineSketch:
        # Real Baseline A (CounterSketch from ES) hooks in here in PR 3.
        # For PR 1 we fall through to SelfSketch (the in-log self-baseline).
        if self._sketch_provider is not None:
            try:
                sk = self._sketch_provider(job, stage)
            except Exception:  # noqa: BLE001 - sketch lookup must never break the filter
                logger.exception("baseline sketch provider failed; using self-baseline")
                sk = None
            if sk is not None:
                return sk
        if self._cfg.use_self_baseline:
            return SelfSketch(lines)
        return EmptySketch()


# ── Helpers ────────────────────────────────────────────────────────────────


def _empty_result(raw_chars: int) -> FilterResult:
    return FilterResult(
        body="",
        fingerprint="",
        primary_error="Unknown",
        primary_location=None,
        locations=(),
        confidence="LOW",
        metadata={"confidence": "LOW", "reason": "empty input"},
        raw_chars=raw_chars,
        body_chars=0,
        body_tokens=0,
    )


def _summarize_collapse(lines: Sequence[Line]) -> dict[str, int]:
    """Count how much each Pass 2 collapser contributed (for the metadata header)."""
    stats = {
        "banner": 0,
        "stack-frame-elided": 0,
        "json-folded": 0,
        "progress-elided": 0,
        "repeat-elided": 0,
    }
    for ln in lines:
        if not ln.drop:
            continue
        if ln.reason in stats:
            stats[ln.reason] += 1
    return stats


def _merge_contributions(lines: list[Line], contribs: Sequence[Contribution]) -> None:
    """Apply every detector's :class:`Contribution` in an order-independent way.

    Score deltas are non-negative additive; drop marks union (subject to
    the severity safety gate); keep_marks override drops. Designed so
    detector evaluation order never affects the final state.
    """
    for c in contribs:
        for idx, delta in c.score_deltas.items():
            if 0 <= idx < len(lines):
                lines[idx].score += max(0, delta)
        for idx in c.drop_marks:
            if 0 <= idx < len(lines):
                ln = lines[idx]
                if ln.severity < Severity.ERROR and not ln.keep:
                    ln.drop = True
                    if not ln.reason:
                        ln.reason = f"detector-drop:{c.detector}"
        for idx in c.keep_marks:
            if 0 <= idx < len(lines):
                lines[idx].keep = True
                lines[idx].drop = False
        for idx, note in c.notes.items():
            if 0 <= idx < len(lines) and not lines[idx].note:
                lines[idx].note = note


def _confidence(
    lines: Sequence[Line],
    selected: Sequence[int],
    primary: FailureLocation | None,
) -> tuple[Confidence, str]:
    """Return ``(confidence_label, low_reason_if_any)``.

    HIGH requires *either*:
      * a surviving line at ERROR severity or above, *and* a primary
        location with kind in ("source", "test", "command") at
        ``confidence >= 0.6``; or
      * a high-confidence source/test location alone (the stack-trace
        detectors are authoritative for their own runtimes — Python
        exception classes like ``NoResultFound`` and ``StopIteration``
        wouldn't otherwise pass the severity gate, but the traceback
        itself is unambiguous evidence of a failure).

    MEDIUM is the fallback when we have *some* signal but not enough to
    claim a precise root cause. LOW means the filter could not produce
    any anchor lines at all.
    """
    if not selected:
        return ("LOW", "no anchor lines met the minimum score threshold")

    has_high_conf_source = (
        primary is not None
        and primary.kind in ("source", "test")
        and primary.confidence >= 0.6
    )

    max_sev = max((lines[i].severity for i in selected), default=Severity.INFO)
    if int(max_sev) < int(Severity.ERROR) and not has_high_conf_source:
        return (
            "LOW",
            "no surviving line scored at ERROR severity or above",
        )

    if primary is None or primary.kind == "unknown":
        return ("MEDIUM", "")

    if primary.kind == "log":
        return ("MEDIUM", "")

    if primary.confidence < 0.6:
        return ("MEDIUM", "")

    return ("HIGH", "")


def _location_to_dict(loc: FailureLocation | None) -> dict | None:
    if loc is None:
        return None
    return {
        "kind": loc.kind,
        "file": loc.file,
        "line": loc.line,
        "column": loc.column,
        "function": loc.function,
        "test_name": loc.test_name,
        "command": loc.command,
        "detector": loc.detector,
        "confidence": loc.confidence,
        "log_line_idx": loc.log_line_idx,
        "message": loc.message,
        "anchor": loc.as_anchor(),
    }


def _tail_excerpt(lines: Sequence[Line], max_chars: int) -> str:
    """Last-resort body for the LOW-confidence fallback.

    Returns the tail of the original (post-tokenize) lines so the LLM
    still has something to ground a response in — even though we just
    told it not to invent a root cause.
    """
    out: list[str] = []
    size = 0
    for ln in reversed(lines):
        if not ln.text:
            continue
        piece = f"L{ln.idx}: {ln.text}"
        if size + len(piece) + 1 > max_chars:
            break
        out.append(piece)
        size += len(piece) + 1
    return "\n".join(reversed(out))
