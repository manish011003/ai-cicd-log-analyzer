"""Render — turn a list of (selected) lines into the LLM-facing body.

Three responsibilities:

* **Citation prefixes.** Every kept line is rendered as ``L<idx>: <text>``
  where ``idx`` is the original log line index. The LLM prompt instructs
  the model to cite ``(L<n>)`` in its analysis; the Web UI uses those
  numbers to deep-link to the raw log viewer.
* **Elision markers.** Where the selector skipped contiguous regions, the
  renderer inserts ``... N lines elided ...`` so the LLM sees that
  context is missing rather than guessing at temporal flow.
* **Metadata header.** Same ``[METADATA]`` block the existing
  ``LogProcessor`` emits, extended with detector / baseline / confidence
  fields. Old consumers that grep for ``Primary Error:`` and ``Exit Code:``
  continue to work.

Also re-exports the small helpers (:func:`primary_error`,
:func:`generate_fingerprint`) that the legacy public surface relies on, so
``filtering/__init__.py`` can pull them from one place.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .base import FailureLocation, Line


# ── Error-signature regex (carried over from the legacy module) ──────────


_EXCEPTION_LINE = re.compile(
    r"\b((?:[\w$]+\.)*[\w$]+(?:Exception|Error|Failure|Fault))\b"
)
_CAUSED_BY = re.compile(r"(?i)Caused\s+by:\s*(.+)")
_ERROR_MARKER = re.compile(r"(?:^|\s)(?:ERROR|FATAL|SEVERE)\b\s*[:\-]?\s*(.+)")
_EXIT_CODE = re.compile(
    r"(?i)(?:exit\s+code|returned\s+exit\s+code|process\s+exited\s+with)"
    r"\s*[:=]?\s*(\d+)"
)


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def primary_error(filtered: str) -> str:
    """``Caused by:`` → exception class → ERROR/FATAL/SEVERE marker → 'Unknown'.

    Same priority as the legacy implementation. Kept as a free function
    rather than a method so external tooling (notebooks, debug scripts)
    can import it standalone.
    """
    return (
        _first(_CAUSED_BY, filtered)
        or _first(_EXCEPTION_LINE, filtered)
        or _first(_ERROR_MARKER, filtered)
        or "Unknown"
    )


def exit_code(text: str) -> str:
    return _first(_EXIT_CODE, text) or "N/A"


def generate_fingerprint(filtered: str, stage_name: str) -> str:
    """Caused-by → first 4 exception types → first ERROR line → stage.

    Unchanged from the legacy contract on purpose: the existing
    Elasticsearch ``failure_solutions`` kNN index was embedded against
    *this* fingerprint shape. Changing it would silently degrade retrieval
    quality for every accepted solution stored before the upgrade.
    """
    caused = _first(_CAUSED_BY, filtered)
    exceptions: list[str] = []
    seen: set[str] = set()
    for m in _EXCEPTION_LINE.finditer(filtered):
        short = m.group(1).rsplit(".", 1)[-1]
        if short not in seen and len(exceptions) < 4:
            seen.add(short)
            exceptions.append(short)

    parts: list[str] = []
    if caused:
        parts.append(f"root_cause: {caused[:200]}")
    if exceptions:
        parts.append("exceptions: " + ", ".join(exceptions))
    if not caused:
        err = _first(_ERROR_MARKER, filtered)
        if err:
            parts.append(f"error: {err[:200]}")
    parts.append(f"stage: {stage_name}")
    return " | ".join(parts)


# ── Body assembly ─────────────────────────────────────────────────────────


def _format_line(ln: Line, *, with_citation: bool) -> str:
    body = ln.text
    if ln.note:
        body = f"{body}  {ln.note}" if body else ln.note
    if with_citation:
        return f"L{ln.idx}: {body}"
    return body


def render_body(
    lines: Sequence[Line],
    selected_idx: Sequence[int],
    *,
    with_citation: bool = True,
) -> str:
    """Concatenate selected lines in original order with elision markers."""
    if not selected_idx:
        return ""
    out: list[str] = []
    last_idx = -1
    for sel in selected_idx:
        ln = lines[sel]
        if last_idx >= 0:
            gap = ln.idx - last_idx - 1
            if gap > 0:
                out.append(f"... {gap} line(s) elided ...")
        out.append(_format_line(ln, with_citation=with_citation))
        last_idx = ln.idx
    return "\n".join(out)


def render_plain_body(lines: Sequence[Line], selected_idx: Sequence[int]) -> str:
    """Legacy ``filter_logs`` rendering: no L<idx>: prefixes.

    Used by the back-compat ``filter_logs(raw) -> str`` callers that pre-date
    the citation contract (``log_processor.py`` shim and a handful of old
    tests). New callers use :func:`render_body` with citations.
    """
    return render_body(lines, selected_idx, with_citation=False)


# ── Metadata header ───────────────────────────────────────────────────────


def render_metadata_header(
    *,
    raw_chars: int,
    body_chars: int,
    body_tokens: int,
    primary: str,
    code: str,
    confidence: str,
    primary_location: FailureLocation | None,
    detectors: Sequence[str],
    baseline_version: str,
    collapse_stats: dict[str, int],
) -> str:
    """Build the ``[METADATA]`` block prepended to the LLM body.

    Keeps the legacy field names (``Primary Error``, ``Exit Code``) so the
    existing test ``test_log_processor_metadata_block_is_present_and_picks_real_primary_error``
    keeps passing without modification.
    """
    ratio = 0.0 if not raw_chars else round(100.0 * (1.0 - body_chars / raw_chars), 1)
    detectors_str = ", ".join(detectors) if detectors else "none"
    location_str = primary_location.as_anchor() if primary_location else "unknown"
    collapse_str = " ".join(
        f"{k}={v}" for k, v in sorted(collapse_stats.items()) if v
    ) or "none"

    return (
        "---\n"
        "[METADATA]\n"
        f"- Primary Error: {primary}\n"
        f"- Exit Code: {code}\n"
        f"- Location: {location_str}\n"
        f"- Confidence: {confidence}\n"
        f"- Detectors: {detectors_str}\n"
        f"- Baseline: {baseline_version}\n"
        f"- Collapse: {collapse_str}\n"
        f"- Compression: {raw_chars} -> {body_chars} chars / ~{body_tokens} tokens "
        f"(~{ratio}% removed)\n"
        "---\n"
    )


def render_low_confidence_banner(reason: str) -> str:
    """Visible marker so the LLM prompt's escape-hatch instruction fires."""
    return (
        "---\n"
        "[FILTER: LOW CONFIDENCE]\n"
        f"Reason: {reason}\n"
        "The filter could not localize the failure with confidence. Treat the\n"
        "log below as best-effort raw context — do not invent a root cause.\n"
        "---\n"
    )
