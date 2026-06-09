"""Pass 1 — tokenize, normalize, classify. Never drops a line.

Every input line becomes one :class:`~failure_analyzer_worker.filtering.base.Line`
with structural metadata: kind (shape), severity, template hash. Later passes
make all their decisions from these annotations; they never re-scan raw text.

Universality is enforced here: the regex bundles classify lines by **shape**
(indentation, stack-frame layout, presence of severity tokens common to every
runtime — ``error``, ``failed``, ``exit``) and never by framework name. The
day someone runs Bazel through this filter, Pass 1 still produces a useful
``list[Line]`` without anyone touching this file.
"""

from __future__ import annotations

import re

from .base import Kind, Line, Severity

# ── Normalizers ────────────────────────────────────────────────────────────
#
# Order matters: ANSI first (so the timestamp regex doesn't trip over colour
# codes), then timestamps, then high-cardinality tokens (UUID before HEX so
# UUIDs aren't partial-matched as hex strings).

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_TIMESTAMP_PREFIX = re.compile(
    r"^(?:"
    # ISO-8601 with optional brackets: 2026-04-06T05:59:53.123Z or [2026-…]
    r"\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\]?\s*"
    r"|"
    # bare wall-clock: 14:30:22.456
    r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+"
    r"|"
    # human date: Apr 6, 2026 5:59:53 AM
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)?\s*"
    r")"
)

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
_NUMS = re.compile(r"\b\d{2,}\b")  # 2+ digit numbers; keeps small ones (line:col, exit 1)
_PATH_UNIX = re.compile(r"(?<![\w/-])/[\w./@\-]+")
_PATH_WIN = re.compile(r"\b[A-Z]:\\[\w.\\@\-]+")
_URL = re.compile(r"https?://\S+")
_SIZE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:[KMGT]i?B|ms|µs|us|s|min)\b", re.IGNORECASE)
_HORIZONTAL_WS = re.compile(r"[^\S\n]+")

# Defensive cap so a pathological multi-MB single-line log (base64 blob,
# minified JS error) can't blow up the regex engine.
_MAX_LINE_CHARS = 2000


# ── Kind detection (shape-based, framework-agnostic) ──────────────────────


_BANNER = re.compile(r"^[\s\-=*#~_]{8,}$|^\s*::\s*Spring Boot\s*::|^\s*\(v\d+\.\d+\.\d+\)\s*$")

# Stack-frame shapes — one per runtime, no framework names.
# Java JPMS frames carry a module prefix like ``java.base/sun.nio.ch.Net.foo``,
# so the charset must include ``/``.
_STACK_JAVA = re.compile(r"^\s+at\s+[\w$./<>]+\([^)]*\)")
_STACK_PY = re.compile(r'^\s+File ".+", line \d+')
_STACK_PY_CALLER = re.compile(r"^\s+\w+\(.*\)\s*$")  # "    foo(x, y)" after the File line
_STACK_GO = re.compile(r"^\s+\S+\.go:\d+(?:\s+\+0x[0-9a-f]+)?\s*$")
_STACK_GO_FN = re.compile(r"^\S+\.\S+\([^)]*\)\s*$")  # "package.Func(args)"
_STACK_RUST = re.compile(r"^\s+\d+:\s+0x[0-9a-f]+")
_STACK_NODE = re.compile(r"^\s+at\s+(?:async\s+)?\S+\s+\([^)]+\)")
_STACK_ELLIPSIS = re.compile(r"^\s*\.{3}\s+\d+\s+more\s*$")  # Java "... 42 more"

# Progress / spinner / downloader chatter. Keyword-based — works on every
# package manager (apt, dnf, npm, pip, gem, cargo, docker), not just Docker.
# Solo occurrences stay; only runs of 4+ collapse in Pass 2.
_PROGRESS = re.compile(
    r"\d+%\s|"
    r"\[[=\->\s\d]*[=>]\s*\d*\s*\]|"  # ASCII progress bars (any width)
    r"\b\d+/\d+\s+(?:packages|tests|files|chunks|steps)\b|"
    r"\b(?:Downloading|Extracting|Pulling|Pushing|Uploading|"
    r"Pull complete|Layer already exists|Already exists|"
    r"Verifying|Status:|Digest:)\b",
    re.IGNORECASE,
)

# Shell command / set-x echo. ``+ ...`` (bash ``set -x``) and ``$ ...`` (typed
# prompt) are unambiguous CI conventions; ``> ...`` is intentionally *not*
# included — npm/yarn/Gradle use it for tool *output*, which would confuse the
# locator. A future ``npm`` / ``gradle`` detector can recover those if needed.
_CMD = re.compile(r"^\s*[+$]\s+\S")

# Single-line key/value (config dumps, env exports).
_KV = re.compile(r"^\s*[A-Za-z_][\w.\-]{1,60}\s*[:=]\s*\S")

# JSON-ish opening line. Must be followed by a quote, digit, or end-of-line —
# this rejects log tags like ``[INFO]``, ``[Pipeline]``, ``[ERROR]`` whose
# leading bracket is *not* the start of a JSON array.
_JSON_OPEN = re.compile(r'^\s*[{\[](?:\s*["\d-]|\s*$)')


def _classify_kind(indented: str, template: str) -> Kind:
    """Classify on the indented form (stacks need ``^\\s+``) but search\n    JSON / kv / cmd on the normalized template (more robust to spacing)."""
    if not template:
        return Kind.PLAIN
    if _BANNER.match(template):
        return Kind.BANNER
    # Stack regexes intentionally match the *indented* line — that's the
    # cheapest way to distinguish "at foo(bar:42)" stack frames from a
    # plain sentence containing the word "at".
    if (
        _STACK_JAVA.match(indented)
        or _STACK_PY.match(indented)
        or _STACK_GO.match(indented)
        or _STACK_RUST.match(indented)
        or _STACK_NODE.match(indented)
        or _STACK_ELLIPSIS.match(indented)
    ):
        return Kind.STACK
    if _PROGRESS.search(template):
        return Kind.PROGRESS
    if _JSON_OPEN.match(template):
        return Kind.JSON
    if _CMD.match(template):
        return Kind.CMD
    if _KV.match(template):
        return Kind.KV
    return Kind.PLAIN


# ── Severity classification (priority-ordered) ────────────────────────────
#
# Each pattern is shape/keyword based, not framework-specific. ``Caused by:``
# (Java), ``raise`` chains (Python), ``panic:`` (Go), ``error[E0382]`` (Rust),
# ``exit 1`` (shell) — all land in CAUSE/FATAL/ERROR by the same rules.

_SEV_RULES: tuple[tuple[re.Pattern[str], Severity], ...] = (
    (re.compile(r"(?i)(?:^|\s)Caused\s+by:\s*\S"), Severity.CAUSE),
    (
        re.compile(
            r"(?i)(?:exit|return)\s*(?:code|status)?\s*[:=]?\s*[1-9]\d*\b|"
            r"script\s+returned\s+exit\s+code\s*[1-9]|"
            r"process\s+exited\s+with\s+(?:code\s+)?[1-9]|"
            r"\bnon-zero\s+exit\b"
        ),
        Severity.EXIT,
    ),
    (
        re.compile(
            r"(?i)\bFATAL\b|\bSEVERE\b|\bpanic(?:ked)?\b|"
            r"\bsegmentation\s+fault\b|\bcore\s+dumped\b"
        ),
        Severity.FATAL,
    ),
    (
        re.compile(
            r"(?i)\bERROR\b|^\s*Error:\s|\[ERROR\]|"
            r"\berror\[[A-Z0-9]+\]|"  # Rust error codes
            r"\bnpm\s+ERR!|"
            r"\b[A-Z]\w+(?:Exception|Error|Failure|Fault)\b"
        ),
        Severity.ERROR,
    ),
    (re.compile(r"(?i)\bWARN(?:ING)?\b|\[WARN\]"), Severity.WARN),
)


def _classify_severity(text: str) -> Severity:
    for rx, sev in _SEV_RULES:
        if rx.search(text):
            return sev
    return Severity.INFO


# ── Normalize a single line and capture the variable tokens it had ────────
#
# We need the captured tokens so the repeat-collapser in Pass 2 can emit
# summaries like "(repeated 5×: 502, 503, 504)" — i.e. show *which* values
# varied across an otherwise-identical retry storm.

_MASKERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_UUID, "UUID"),
    (_URL, "URL"),
    (_PATH_UNIX, "PATH"),
    (_PATH_WIN, "PATH"),
    (_HEX, "HEX"),
    (_SIZE, "SIZE"),
    (_NUMS, "N"),
)


def _normalize(text: str) -> tuple[str, str, tuple[str, ...]]:
    """Strip ANSI + collapse whitespace; return display + template + var tokens.

    Two forms are produced:

    * ``display`` — human-readable, used by the renderer and the
      :class:`Line.text` field. Preserves paths, file:line, URLs, and
      numeric values because those are exactly the content a developer
      reading the filtered log most cares about.
    * ``template`` — high-cardinality tokens masked to fixed labels
      (``PATH``, ``URL``, ``UUID``, ``HEX``, ``SIZE``, ``N``). Used only
      to compute ``template_hash`` for repeat-dedupe and baseline lookups
      — not shown to anyone.

    ``var_tokens`` lists the original values that were masked into the
    template, so the repeat-summary renderer can surface them (e.g.
    ``values=502, 503, 504`` for an HTTP retry storm).
    """
    out = _ANSI.sub("", text)
    out = _HORIZONTAL_WS.sub(" ", out).strip()
    display = out

    captured: list[str] = []
    template = out
    for rx, label in _MASKERS:

        def _sub(m: re.Match[str], _lbl: str = label) -> str:
            captured.append(m.group(0))
            return _lbl

        template = rx.sub(_sub, template)

    return display, template, tuple(captured)


# ── Public entry point ────────────────────────────────────────────────────


def tokenize(raw_text: str) -> list[Line]:
    """Return one :class:`Line` per input line.

    Idempotent and pure: no I/O, no caching, no global state. Safe to call
    from a worker thread.
    """
    if not raw_text:
        return []

    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines: list[Line] = []

    for i, raw in enumerate(text.split("\n")):
        # Strip timestamps before length cap so we don't waste the budget on
        # a 200-char leading timestamp prefix.
        stripped = _TIMESTAMP_PREFIX.sub("", raw)
        if len(stripped) > _MAX_LINE_CHARS:
            stripped = stripped[:_MAX_LINE_CHARS] + " <truncated>"

        indent = len(stripped) - len(stripped.lstrip(" \t"))
        display, template, captured = _normalize(stripped)

        if not display:
            # Preserve blank lines as PLAIN with empty text — the renderer
            # uses these to keep visual breaks where the original log had
            # paragraph boundaries.
            lines.append(Line(idx=i, raw=raw, text="", kind=Kind.PLAIN))
            continue

        lines.append(
            Line(
                idx=i,
                raw=raw,
                text=display,
                kind=_classify_kind(stripped, template),
                severity=_classify_severity(display),
                indent=indent,
                template_hash=hash(template),
                var_tokens=captured,
            )
        )

    return lines
