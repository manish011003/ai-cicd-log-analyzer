"""Parameterised stack-trace detector — language config in, detector out.

``GenericStackDetector`` is the data-driven cousin of the hand-written
``java_stack`` / ``python_traceback`` detectors. The runtime behaviour is
identical (same :class:`Detector` protocol, same ``Contribution`` merge,
same severity safety gate), but the regexes and conventions are supplied
as a :class:`StackLanguage` config object instead of being baked into the
class.

This is the **stepping stone to Layer 2** (declarative YAML detectors): a
YAML loader will eventually read a spec file off disk, hydrate a
``StackLanguage`` from it, and hand back ``GenericStackDetector(spec)``.
Until then, each bundled language gets a thin module under
``detectors/`` that constructs its ``StackLanguage`` in Python and calls
:func:`register` — see ``node_stack.py`` and ``go_panic.py``.

Why the two hand-written detectors stay separate:

* ``java_stack`` knows JPMS module prefixes, a curated 30-entry
  third-party framework allowlist, and a Caused-by chain walker — too
  much language-specific nuance to flatten into config without losing
  precision.
* ``python_traceback`` handles chained ``during handling of`` runs and
  the body-source snippet Python prints under each ``File`` entry — both
  require algorithmic logic that doesn't fit the regex-only model below.

For the simpler "match a frame, skip framework paths, pick top or bottom
project frame" pattern that covers Node, Go, Ruby, .NET, and most
test-runner stack traces, this generic detector is plenty.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from ..base import Contribution, FailureLocation, Line


@dataclass(frozen=True, slots=True)
class StackLanguage:
    """Declarative description of one runtime's stack-frame conventions.

    Designed so a future YAML loader can populate every field from a
    plain-data spec without subclassing or code generation. All regex
    fields are kept as strings (not compiled patterns) for the same
    reason — the YAML loader can hand them straight to the constructor.
    """

    # Identity
    name: str  # registered detector name (e.g. "node_stack")
    priority: int  # tie-breaker for the locator; 50 is the standard for stack detectors
    description: str  # one-line description shown on the Settings page

    # Activation probe. Run against the first 4 KB of raw log (cheap).
    # False positives are fine — :meth:`contribute` re-validates by
    # actually matching ``frame_pattern`` against full lines.
    probe_pattern: str

    # Per-frame regex. MUST contain named groups ``file`` and ``line``.
    # MAY contain named groups ``function`` and ``column``. The match is
    # run against ``Line.raw`` (preserves indentation) so leading-whitespace
    # anchors like ``^\s+at`` keep working.
    frame_pattern: str

    # Optional "loose" regex used only to keep a frame run alive across
    # decorative lines that *look* like frames but don't carry a file:line
    # we can extract. Two real-world examples that need this:
    #
    # * V8's ``    at async Promise.all (index 0)`` — frame-shaped, no file.
    # * Go's function-call line between file:line frames
    #   (``main.processPayment(0x0)`` sits between two ``handler.go:42`` lines).
    #
    # Lines matching this pattern but NOT ``frame_pattern`` don't contribute
    # a frame — they just prevent the run from breaking. Defaults to empty
    # (strict mode, identical to the pre-change behaviour).
    run_separator_pattern: str = ""

    # File-path fragments that mark a frame as framework / standard-library /
    # package-manager code. Frames whose ``file`` group contains any of these
    # substrings are excluded from the "project frame" walk. Empty tuple
    # means "every frame is project code" — fine for languages with no
    # widely-shared package directories.
    framework_paths: tuple[str, ...] = ()

    # "top"     — bug site is the first project frame walking from index 0.
    #             Use for runtimes that print the deepest call first
    #             (Java, Kotlin, Scala, Node, Go, .NET, Ruby).
    # "bottom"  — bug site is the last project frame walking from index 0.
    #             Use for runtimes that print the most recent call last
    #             (Python).
    bug_site: Literal["top", "bottom"] = "top"

    # Go convention: the line matched by ``frame_pattern`` is the
    # ``file:line`` line, and the function name is on the line immediately
    # above. When True, we try to recover the function name from the prior
    # non-blank line before falling back to the file basename.
    function_on_previous_line: bool = False

    # Optional cause/error-class regex. When set, used to extract the
    # exception class + message into the emitted ``FailureLocation.message``
    # and to annotate the bug-site frame with a ``<-- <cls>`` note. Should
    # contain a named group ``cls`` and optionally ``msg``.
    cause_pattern: str = ""
    cause_position: Literal["above", "below"] = "above"  # where the cause line sits relative to the stack
    cause_lookback: int = 8  # how many non-frame lines to scan in that direction

    # Confidence model (mirrors java_stack._confidence_for).
    top_confidence: float = 0.85
    confidence_decay: float = 0.10  # subtract per additional project frame
    min_confidence: float = 0.55  # floor so deeper frames stay above MEDIUM
    fallback_confidence: float = 0.45  # used when only framework frames are present

    # Per-frame score boosts merged into Pass 4 ranking. Top project frame
    # gets ``top_boost``, others get ``frame_boost``. Tuned to match the
    # values the hand-written detectors use today.
    top_boost: int = 3
    frame_boost: int = 2
    cause_boost: int = 2

    # Internal — derived once and cached, to keep ``contribute`` allocation-free.
    _compiled: dict = field(default_factory=dict, repr=False, compare=False)


class GenericStackDetector:
    """Data-driven :class:`Detector` for languages whose failure shape fits
    a ``(probe + frame regex + framework path list + bug-site rule)`` tuple.

    Multiple instances of this class can be registered side by side — each
    with its own :class:`StackLanguage`. The orchestrator treats them as
    independent detectors (one ``Contribution`` each, merged commutatively
    against everything else).
    """

    def __init__(self, lang: StackLanguage) -> None:
        self._lang = lang
        self.name = lang.name
        self.priority = lang.priority
        self.description = lang.description  # picked up by describe_filter()
        self._probe = re.compile(lang.probe_pattern, re.MULTILINE)
        self._frame = re.compile(lang.frame_pattern)
        self._run_sep = (
            re.compile(lang.run_separator_pattern)
            if lang.run_separator_pattern
            else None
        )
        self._cause = re.compile(lang.cause_pattern) if lang.cause_pattern else None

    # ── Detector protocol ──

    def activates_on(self, probe: str) -> bool:
        return self._probe.search(probe) is not None

    def contribute(self, lines: Sequence[Line]) -> Contribution | None:
        # 1. Collect contiguous runs of frame-matching lines.
        runs = self._collect_runs(lines)
        if not runs:
            return None

        score_deltas: dict[int, int] = {}
        keep_marks: set[int] = set()
        notes: dict[int, str] = {}
        locations: list[FailureLocation] = []

        for run in runs:
            self._contribute_run(
                lines,
                run,
                score_deltas=score_deltas,
                keep_marks=keep_marks,
                notes=notes,
                locations=locations,
            )

        if not locations and not keep_marks:
            return None

        return Contribution(
            detector=self.name,
            score_deltas=score_deltas,
            keep_marks=frozenset(keep_marks),
            locations=tuple(locations),
            notes=notes,
        )

    # ── Internals ──

    def _collect_runs(
        self, lines: Sequence[Line]
    ) -> list[list[tuple[int, re.Match[str]]]]:
        """Group consecutive frame-matching lines into runs.

        A run is broken by any line that matches neither ``frame_pattern``
        nor (when set) ``run_separator_pattern``. Lines matching the
        separator but not the strict frame pattern keep the run alive
        without contributing a frame — that's what lets V8's
        ``at async Promise.all (index 0)`` and Go's function-name lines
        sit between extractable frames without splitting them into
        single-frame runs that would defeat the bug-site walk.
        """
        runs: list[list[tuple[int, re.Match[str]]]] = []
        current: list[tuple[int, re.Match[str]]] = []
        for i, ln in enumerate(lines):
            m = self._frame.match(ln.raw)
            if m is not None:
                current.append((i, m))
                continue
            if current and self._run_sep is not None and self._run_sep.match(ln.raw):
                # Frame-shaped filler — keep the run alive, don't emit a frame.
                continue
            if current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)
        return runs

    def _is_framework(self, file: str) -> bool:
        if not file:
            return True  # frames missing a file path are treated as opaque/framework
        return any(hint in file for hint in self._lang.framework_paths)

    def _find_function_above(
        self, lines: Sequence[Line], frame_idx: int
    ) -> str:
        """Walk up from a Go-style file:line frame to its function signature.

        Stops at the first non-blank line. Returns an empty string if the
        candidate doesn't look like a function call (``pkg.Fn(args)``).
        """
        for k in range(frame_idx - 1, max(-1, frame_idx - 4), -1):
            text = lines[k].text
            if not text:
                continue
            # Lightweight sniff: must contain a dot (package separator) and
            # an opening paren. Tighter than _STACK_GO_FN to avoid grabbing
            # ``goroutine 1 [running]:`` lines.
            if "." in text and "(" in text:
                return text.split("(", 1)[0].strip()
            return ""
        return ""

    def _find_cause(
        self,
        lines: Sequence[Line],
        run: list[tuple[int, re.Match[str]]],
    ) -> tuple[str, str, int]:
        """Return ``(cls, msg, log_line_idx)`` from the cause line, or empties.

        ``cause_position="above"`` scans the ``cause_lookback`` lines
        directly above the run's top frame; ``"below"`` scans the same
        many lines after the run's last frame. Blank lines are skipped
        but counted against the lookback budget.
        """
        if self._cause is None:
            return ("", "", -1)

        if self._lang.cause_position == "above":
            top = run[0][0]
            window = range(top - 1, max(-1, top - 1 - self._lang.cause_lookback), -1)
        else:
            bottom = run[-1][0]
            window = range(
                bottom + 1, min(len(lines), bottom + 1 + self._lang.cause_lookback)
            )

        for k in window:
            text = lines[k].text
            if not text:
                continue
            m = self._cause.search(text)
            if not m:
                continue
            groups = m.groupdict()
            cls = (groups.get("cls") or "").strip()
            msg = (groups.get("msg") or "").strip()[:160]
            return (cls, msg, lines[k].idx)

        return ("", "", -1)

    def _contribute_run(
        self,
        lines: Sequence[Line],
        run: list[tuple[int, re.Match[str]]],
        *,
        score_deltas: dict[int, int],
        keep_marks: set[int],
        notes: dict[int, str],
        locations: list[FailureLocation],
    ) -> None:
        cause_cls, cause_msg, cause_idx = self._find_cause(lines, run)
        if cause_idx >= 0:
            # The cause line itself is interesting — boost + keep so the
            # anchor cluster picks it up alongside the bug-site frame.
            # Map cause_idx (log idx) back to the lines[] position.
            for k, ln in enumerate(lines):
                if ln.idx == cause_idx:
                    score_deltas[k] = score_deltas.get(k, 0) + self._lang.cause_boost
                    keep_marks.add(k)
                    break

        # Per-frame walk. The iteration order depends on bug_site, so the
        # *first* project frame we encounter in that order is always the
        # bug site (regardless of language convention).
        ordered = run if self._lang.bug_site == "top" else list(reversed(run))

        project_offset = 0
        first_project_idx: int | None = None
        emitted_any = False

        for (i, m) in ordered:
            groups = m.groupdict()
            file = (groups.get("file") or "").strip()
            line_no = int(groups.get("line") or 0)
            function = (groups.get("function") or "").strip()
            column = int(groups.get("column") or 0) if (groups.get("column") or "").isdigit() else 0

            # Go-style: function name lives on the line above the file:line frame.
            if not function and self._lang.function_on_previous_line:
                function = self._find_function_above(lines, i)

            if self._is_framework(file):
                continue

            confidence = self._confidence_for(project_offset, file, line_no)
            is_first_project = first_project_idx is None
            project_offset += 1

            loc = FailureLocation(
                kind="source",
                file=file,
                line=line_no,
                column=column,
                function=function,
                detector=self.name,
                confidence=confidence,
                log_line_idx=lines[i].idx,
                message=(
                    f"{cause_cls}: {cause_msg}".rstrip(": ")
                    if cause_cls
                    else (function or file)
                ),
            )
            locations.append(loc)
            emitted_any = True

            boost = self._lang.top_boost if is_first_project else self._lang.frame_boost
            score_deltas[i] = score_deltas.get(i, 0) + boost
            keep_marks.add(i)

            if is_first_project:
                first_project_idx = i
                if cause_cls and not notes.get(i):
                    tail = f": {cause_msg}" if cause_msg else ""
                    notes[i] = f"<-- {cause_cls}{tail}"

        # Pure-framework run? Emit one fallback location so the locator
        # still has *something* to anchor the UI deep-link on, but at a
        # confidence well below the HIGH gate (0.6) — the bug is upstream
        # of every frame we saw.
        if not emitted_any:
            first_i, first_m = run[0] if self._lang.bug_site == "top" else run[-1]
            groups = first_m.groupdict()
            file = (groups.get("file") or "").strip()
            line_no = int(groups.get("line") or 0)
            function = (groups.get("function") or "").strip()
            if not function and self._lang.function_on_previous_line:
                function = self._find_function_above(lines, first_i)
            locations.append(
                FailureLocation(
                    kind="source",
                    file=file,
                    line=line_no,
                    function=function,
                    detector=self.name,
                    confidence=self._lang.fallback_confidence,
                    log_line_idx=lines[first_i].idx,
                    message=cause_cls or "framework-internal stack",
                )
            )
            score_deltas[first_i] = score_deltas.get(first_i, 0) + 1
            keep_marks.add(first_i)

    def _confidence_for(self, offset: int, file: str, line: int) -> float:
        """Top project frame → ``top_confidence``; decays per project depth.

        Frames missing file or line metadata are capped because the locator
        cannot deep-link them in the UI — a high-confidence location with
        nothing to click is misleading.
        """
        lang = self._lang
        base = (
            lang.top_confidence
            if offset == 0
            else max(lang.min_confidence, lang.top_confidence - lang.confidence_decay * offset)
        )
        if not file:
            return min(base, 0.50)
        if line <= 0:
            return min(base, 0.65)
        return base
