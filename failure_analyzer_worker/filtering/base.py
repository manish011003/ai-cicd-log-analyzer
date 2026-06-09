"""Filter protocols + value objects.

The rest of the codebase depends only on :class:`Filter` (the universal
pipeline) and :class:`Detector` (the plug-and-play extension point).
Concrete detectors live under ``detectors/`` and follow the same provider
pattern as ``llm/providers/`` and ``vectorstore/providers/``.

Design constraints baked into these types:

* :class:`Line` records carry their original index so every kept line can
  be cited as ``L<idx>:`` in the LLM body and deep-linked from the UI.
* :class:`Detector` returns immutable :class:`Contribution` records rather
  than mutating ``Line`` in place — the orchestrator merges them
  deterministically so detector order never matters.
* :class:`FilterResult` exposes ``body`` as a plain string for wire-format
  compatibility with the existing Postgres ``filtered_logs`` column and the
  Web UI. Structured fields (locations, metadata) are additive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Literal, Protocol, Sequence, runtime_checkable


# ── Line model carried through every pass ──────────────────────────────────


class Kind(IntEnum):
    """Structural shape of a log line. Detected once in Pass 1.

    Ordering reflects rough priority for collapsing: higher kinds are more
    likely to be folded into summary lines (stacks, JSON blobs, progress
    bursts, banners).
    """

    PLAIN = 0
    CMD = 1
    KV = 2
    JSON = 3
    STACK = 4
    PROGRESS = 5
    BANNER = 6


class Severity(IntEnum):
    """Six-level severity scale used by scoring and the baseline gate.

    The gap between ``WARN`` and ``ERROR`` is deliberate: anything at or
    above ``ERROR`` is exempt from baseline-driven drops to defend against
    sketch poisoning (chronically-broken jobs leaking real errors into the
    "normal" baseline).
    """

    INFO = 0
    DEBUG = 0
    WARN = 1
    ERROR = 3
    FATAL = 4
    CAUSE = 5
    EXIT = 5


@dataclass(slots=True)
class Line:
    """One tokenized log line plus the metadata every pass annotates.

    ``raw`` is the original input line, ``text`` is the normalized form
    (timestamps stripped, identifiers masked) used for hashing and
    scoring. Both are kept so the renderer can show the human-readable
    form while passes reason about the canonical form.
    """

    idx: int
    raw: str
    text: str
    kind: Kind = Kind.PLAIN
    severity: Severity = Severity.INFO
    indent: int = 0
    template_hash: int = 0
    score: int = 0
    drop: bool = False
    keep: bool = False  # explicit force-keep wins over drop
    reason: str = ""  # why the renderer kept it (anchor, context, summary, ...)
    note: str = ""  # optional annotation appended on render (e.g. "(repeated 14×)")
    var_tokens: tuple[str, ...] = ()  # captured masked values for repeat summaries


# ── Structured failure location (goal 2 deliverable) ──────────────────────


LocationKind = Literal["source", "test", "command", "log", "unknown"]


@dataclass(frozen=True, slots=True)
class FailureLocation:
    """Where the failure happened, as structured data.

    The filter never returns a prose anchor on its own — callers either
    get a :class:`FailureLocation` they can render as a click target or
    an explicit ``kind="unknown"`` admitting the listener-supplied log
    did not contain enough signal to localize.
    """

    kind: LocationKind
    file: str = ""
    line: int = 0
    column: int = 0
    function: str = ""
    test_name: str = ""
    command: str = ""
    detector: str = "core"
    confidence: float = 0.0  # 0..1; orchestrator picks the highest
    log_line_idx: int = -1  # always set when known; UI deep-link target
    message: str = ""  # short human-readable summary

    def as_anchor(self) -> str:
        """Render as a single-line UI label (e.g. 'Foo.java:42 -- cannot find symbol').

        Uses ASCII-only separators so the metadata header round-trips
        through Windows cp1252 consoles without ``UnicodeEncodeError``.
        """
        sep = " -- "
        if self.kind == "source" and self.file:
            loc = f"{self.file}:{self.line}" + (f":{self.column}" if self.column else "")
            head = f"{self.function} @ {loc}" if self.function else loc
            return f"{head}{sep}{self.message}" if self.message else head
        if self.kind == "test" and self.test_name:
            return (
                f"{self.test_name}{sep}{self.message}" if self.message else self.test_name
            )
        if self.kind == "command" and self.command:
            return (
                f"$ {self.command}{sep}{self.message}" if self.message else f"$ {self.command}"
            )
        if self.kind == "log" and self.log_line_idx >= 0:
            return (
                f"line {self.log_line_idx}{sep}{self.message}"
                if self.message
                else f"line {self.log_line_idx}"
            )
        return self.message or "unknown"


# ── Detector contributions (immutable; order-independent) ──────────────────


@dataclass(frozen=True, slots=True)
class Contribution:
    """What a detector adds to the merged decision set.

    All four fields are *additive*: deltas accumulate, drop and keep marks
    are unioned. The orchestrator never lets a detector reduce a score or
    un-drop a previously-dropped line — that property is what makes the
    detector phase commutative and lets us run detectors in any order
    (or in parallel) without changing the result.
    """

    detector: str
    score_deltas: dict[int, int] = field(default_factory=dict)
    drop_marks: frozenset[int] = field(default_factory=frozenset)
    keep_marks: frozenset[int] = field(default_factory=frozenset)
    locations: tuple[FailureLocation, ...] = ()
    notes: dict[int, str] = field(default_factory=dict)


# ── Filter output ──────────────────────────────────────────────────────────


Confidence = Literal["HIGH", "MEDIUM", "LOW"]


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Everything the worker needs from one filter run.

    ``body`` is the LLM-facing text and stays a plain string so existing
    consumers (Postgres ``filtered_logs`` column, Web UI session view,
    legacy chat flow) need no schema change. Structured fields are
    additive and only new consumers (the locator UI, observability
    dashboard) need to read them.
    """

    body: str
    fingerprint: str
    primary_error: str
    primary_location: FailureLocation | None
    locations: tuple[FailureLocation, ...]
    confidence: Confidence
    metadata: dict
    raw_chars: int
    body_chars: int
    body_tokens: int


# ── Protocols ──────────────────────────────────────────────────────────────


@runtime_checkable
class Filter(Protocol):
    """Universal log filter: raw stage log → :class:`FilterResult`.

    Implementations compose the four passes (tokenize, structural,
    baseline, anchors) plus the detector phase. The protocol surface is
    deliberately narrow so the worker route can swap implementations
    (e.g. a future remote filter service) without touching ``graph.py``.
    """

    def filter(  # pragma: no cover - protocol
        self,
        raw_logs: str,
        *,
        stage_name: str = "",
        job_name: str = "",
    ) -> FilterResult: ...


@runtime_checkable
class Detector(Protocol):
    """Stack-specific plug-in that contributes drops, scores, and locations.

    Two-stage activation:

    * :meth:`activates_on` is a cheap shape-based probe (typically a
      compiled regex on the first 4 KB of the log). It runs against every
      log and *may* false-positive — that's fine.
    * :meth:`contribute` is the real work. It returns ``None`` if the
      cheap probe was a false positive (i.e. the detector's characteristic
      content was not actually present), so probe noise never pollutes
      results.

    Detectors are **pure functions** of ``lines``. They never mutate the
    input list, never read state outside their arguments, and never raise
    out of :meth:`contribute` (the orchestrator wraps each call defensively
    so a buggy detector cannot take down the filter).
    """

    name: str
    priority: int  # tie-breaker for locator picks (higher wins)

    def activates_on(self, probe: str) -> bool: ...  # pragma: no cover - protocol

    def contribute(  # pragma: no cover - protocol
        self,
        lines: Sequence[Line],
    ) -> "Contribution | None": ...
