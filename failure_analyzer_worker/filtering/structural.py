"""Pass 2 — structural collapse. Drops by shape, never by vocabulary.

Four collapsers run in fixed order:

1. **Banners** — punctuation-only and Spring Boot ``::`` banner lines.
2. **Stacks** — keep top frame + first N project frames, replace the rest
   with one ``... K frames elided`` summary; cause-chains (``Caused by:``)
   are preserved because each becomes a *separate* stack run.
3. **JSON / multiline payloads** — fold balanced ``{...}`` / ``[...]``
   blocks longer than a few lines into one summary with the top-level
   keys preserved.
4. **Progress bursts** — runs of ``kind=PROGRESS`` collapse to a single
   summary line carrying the run's head tokens.
5. **Repeat dedupe** — sliding-window collapse by ``template_hash``: keep
   first + last occurrence, annotate the count and the distinct variable
   values that varied between them (``: values=502,503,504``).

All four mark ``Line.drop = True``; they never delete from the list, so
``Line.idx`` keeps pointing at the original log line and citations stay
stable. The safety gate ``severity >= ERROR`` exempts a line from every
collapser except banners — defending against retry-storm dedupes
swallowing the actual error.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Sequence

from .base import Kind, Line, Severity

# Package-manager / runtime directories that mark a stack frame as "framework",
# not "project code". Universal enough to span Java/Python/Node/Go/Rust without
# naming any specific framework.
_PKG_MGR_HINTS: tuple[str, ...] = (
    "/site-packages/",
    "/node_modules/",
    "/.m2/",
    "/.gradle/",
    "/.cargo/",
    "/usr/lib/",
    "/usr/local/",
    "/opt/",
    "vendor/",
    "/gems/",
    "java.base/",
    "java.lang.",
    "sun.nio.",
    "sun.reflect.",
    "jdk.internal.",
)


def _is_project_frame(text: str) -> bool:
    return not any(hint in text for hint in _PKG_MGR_HINTS)


# ── Banners ───────────────────────────────────────────────────────────────


def _drop_banners(lines: list[Line]) -> None:
    for ln in lines:
        if ln.kind == Kind.BANNER:
            ln.drop = True
            ln.reason = "banner"


# ── Stacks ────────────────────────────────────────────────────────────────


def _collapse_stacks(lines: list[Line], keep_project: int = 3) -> None:
    """Walk consecutive STACK lines; keep top + first N project frames.

    The throwing line (the non-stack line *above* the run) is not touched
    here; it carries the exception type and its score will be evaluated
    independently in Pass 4.
    """
    n = len(lines)
    i = 0
    while i < n:
        if lines[i].kind != Kind.STACK or lines[i].drop:
            i += 1
            continue

        # Find the end of this run.
        j = i
        while j < n and lines[j].kind == Kind.STACK:
            j += 1

        run = range(i, j)
        # Small runs: leave them alone (visual cost of the summary line
        # exceeds the savings).
        if (j - i) <= max(4, keep_project + 1):
            i = j
            continue

        keep_local: set[int] = {0}  # always keep top frame
        project_seen = 0
        for offset in range(1, j - i):
            ln = lines[i + offset]
            if _is_project_frame(ln.text) and project_seen < keep_project:
                keep_local.add(offset)
                project_seen += 1

        # Boost the kept frames so the Pass 4 cluster around the throwing
        # line absorbs them — without this they'd score 0 (INFO + stack
        # penalty) and the round-robin allocator would skip them.
        lines[i].score += 3  # top frame: essential
        for off in keep_local - {0}:
            lines[i + off].score += 2  # project frames: very helpful

        elided = 0
        last_kept_offset = max(keep_local)
        for offset in range(j - i):
            if offset in keep_local:
                continue
            lines[i + offset].drop = True
            lines[i + offset].reason = "stack-frame-elided"
            elided += 1

        if elided > 0:
            tail = lines[i + last_kept_offset]
            tail.note = f"... {elided} frame(s) elided ..."

        i = j


# ── JSON / multiline payloads ─────────────────────────────────────────────


_JSON_KEY = re.compile(r'^\s*"([^"]{1,60})"\s*:')


def _fold_json(lines: list[Line], max_run: int = 200, min_run: int = 5) -> None:
    """Collapse balanced JSON / multiline payload blocks.

    Tracks bracket balance starting from the JSON opener; bails out if the
    run exceeds ``max_run`` lines (defensive against unbalanced or pretty-
    printed dumps that go on forever).
    """
    n = len(lines)
    i = 0
    while i < n:
        ln = lines[i]
        if ln.kind != Kind.JSON or ln.drop or not ln.text:
            i += 1
            continue

        depth = ln.text.count("{") + ln.text.count("[") - ln.text.count("}") - ln.text.count("]")
        if depth <= 0:
            # Single-line JSON — leave it alone.
            i += 1
            continue

        j = i + 1
        top_keys: list[str] = []
        while j < n and depth > 0 and (j - i) < max_run:
            t = lines[j].text
            if t:
                depth += t.count("{") + t.count("[") - t.count("}") - t.count("]")
                if len(top_keys) < 5:
                    m = _JSON_KEY.match(t)
                    if m:
                        top_keys.append(m.group(1))
            j += 1

        run_len = j - i
        if run_len >= min_run:
            for k in range(i + 1, j):
                lines[k].drop = True
                lines[k].reason = "json-folded"
            keys = ",".join(top_keys) if top_keys else "?"
            ln.note = f"<json: {run_len} lines, top-keys=[{keys}]>"
            ln.reason = "json-summary"

        i = j


# ── Progress bursts ───────────────────────────────────────────────────────


def _collapse_progress(lines: list[Line], min_run: int = 4) -> None:
    n = len(lines)
    i = 0
    while i < n:
        if lines[i].kind != Kind.PROGRESS or lines[i].drop:
            i += 1
            continue

        j = i
        while j < n and lines[j].kind == Kind.PROGRESS:
            j += 1

        run_len = j - i
        if run_len >= min_run:
            # Use the head tokens of the first line as the human label.
            head = " ".join(lines[i].text.split()[:2]) or "progress"
            for k in range(i + 1, j):
                lines[k].drop = True
                lines[k].reason = "progress-elided"
            lines[i].note = f"<progress: {run_len} lines elided ({head})>"
            lines[i].reason = "progress-summary"

        i = j


# ── Repeat dedupe (sliding window by template_hash) ───────────────────────


def _summarize_var_tokens(occurrences: list[Line]) -> str:
    """Build the ``: values=...`` suffix from variable tokens seen in repeats.

    Picks at most three distinct values across all occurrences so an HTTP
    status escalation ``502, 503, 504`` survives the collapse even though
    the templates hashed equal. Each individual value is capped at 40
    chars so a long URL or path doesn't dominate the summary line.
    """
    seen: list[str] = []
    for ln in occurrences:
        for tok in ln.var_tokens:
            # We mostly want the *actual* values, not the masked labels.
            # _normalize captured the originals, so prefer those.
            if tok in ("PATH", "URL", "UUID", "HEX", "SIZE", "N"):
                continue
            short = tok if len(tok) <= 40 else tok[:37] + "..."
            if short in seen:
                continue
            seen.append(short)
            if len(seen) >= 3:
                return ", ".join(seen)
    return ", ".join(seen)


def _dedupe_repeats(
    lines: list[Line], window: int = 50, min_count: int = 3
) -> None:
    """Within a rolling window of ``window`` lines, collapse identical templates.

    Keep first and last occurrence; mark interior ones for drop. Severity
    >= ERROR is always exempt — a real error line never disappears into a
    repeat summary even if its template hash matched.
    """
    by_hash: dict[int, list[int]] = {}
    window_q: deque[int] = deque()

    def _flush(h: int) -> None:
        idxs = by_hash.get(h, [])
        if len(idxs) < min_count:
            return
        first, last = idxs[0], idxs[-1]
        occurrences = [lines[i] for i in idxs]
        for mid in idxs[1:-1]:
            lines[mid].drop = True
            lines[mid].reason = "repeat-elided"
        tokens = _summarize_var_tokens(occurrences)
        span = last - first + 1
        suffix = f": values={tokens}" if tokens else ""
        lines[last].note = f"(repeated {len(idxs)}x over {span} lines{suffix})"
        lines[last].reason = "repeat-summary"

    for i, ln in enumerate(lines):
        if ln.drop or not ln.text or ln.severity >= Severity.ERROR:
            # ERROR and above are exempt: never let a repeat-storm hide a
            # real failure line.
            continue
        h = ln.template_hash
        if h == 0:
            continue
        by_hash.setdefault(h, []).append(i)
        window_q.append(i)
        # Evict anything that fell out of the window.
        while window_q and (i - window_q[0]) > window:
            old = window_q.popleft()
            old_h = lines[old].template_hash
            bucket = by_hash.get(old_h)
            if bucket and bucket[0] == old:
                # This is the oldest occurrence — finalize this bucket if it
                # accumulated enough hits before sliding off.
                _flush(old_h)
                del by_hash[old_h]

    # Flush remaining buckets at end-of-input.
    for h in list(by_hash):
        _flush(h)


# ── Public entry point ────────────────────────────────────────────────────


def collapse(lines: Sequence[Line]) -> None:
    """Apply all four collapsers in place. Order matters — keep as-is."""
    ls = list(lines) if not isinstance(lines, list) else lines
    _drop_banners(ls)
    _collapse_stacks(ls)
    _fold_json(ls)
    _collapse_progress(ls)
    _dedupe_repeats(ls)
