"""Detector — CPython tracebacks (``Traceback (most recent call last):``).

Activates on any log carrying at least one ``File "...", line N, in <fn>``
record. Emits one :class:`FailureLocation` per *project* frame, with the
**bottom-most** project frame getting the highest confidence — Python's
convention is the inverse of Java's, so the deepest call is at the bottom
and that's where the exception was raised.

Three details the detector handles that a naive regex would miss:

* Chained tracebacks (``During handling of the above exception, another
  exception occurred:``) — each sub-traceback is parsed independently and
  contributes its own bottom-project frame.
* Multiple-line File entries (the next line is the offending source code).
  We don't emit a location for that body line; we annotate the File line
  with the source snippet as a note for the LLM body.
* Exception summary at the end (``ValueError: bad arg``) — used as the
  ``message`` on the emitted location.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..base import Contribution, FailureLocation, Line
from . import register


_PROBE = re.compile(r'^\s+File ".+", line \d+', re.MULTILINE)

_FRAME = re.compile(
    r'^\s+File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<function>\S+))?\s*$'
)

# Traceback start markers — the canonical one, plus the two chained-exception
# bridges Python emits between sub-tracebacks.
_TRACEBACK_START = re.compile(
    r"^Traceback \(most recent call last\):\s*$"
    r"|^During handling of the above exception, another exception occurred:\s*$"
    r"|^The above exception was the direct cause of the following exception:\s*$"
)

# Exception summary at the end of a traceback: optionally module-qualified
# class (e.g. ``sqlalchemy.exc.NoResultFound``), optional colon-prefixed
# message. We accept any uppercase-starting class — Python exception names
# are inconsistent (``NoResultFound``, ``StopIteration``, ``KeyboardInterrupt``,
# ``OSError``…). False positives are limited because this regex is only
# searched within the tail window right after the last File-frame.
_EXCEPTION_SUMMARY = re.compile(
    r"^(?P<cls>(?:[\w]+\.)*[A-Z][\w]*)"
    r"(?::\s*(?P<msg>.*))?$"
)


# Framework hints — Python-specific. Anything containing one of these path
# fragments is treated as third-party / standard-library code.
_FRAMEWORK_PATH_HINTS: tuple[str, ...] = (
    "/site-packages/",
    "/dist-packages/",
    "\\site-packages\\",
    "\\dist-packages\\",
    "/python3.",
    "\\python3",
    "\\Lib\\",
    "/usr/lib/python",
    "/usr/local/lib/python",
    "<frozen ",
    "<built-in",
    "<string>",
)


def _is_framework_file(file: str) -> bool:
    if not file:
        return True
    return any(hint in file for hint in _FRAMEWORK_PATH_HINTS)


class PythonTracebackDetector:
    """Localizes Python failures to the deepest project frame."""

    name = "python_traceback"
    priority = 50

    def activates_on(self, probe: str) -> bool:
        return _PROBE.search(probe) is not None

    def contribute(self, lines: Sequence[Line]) -> Contribution | None:
        # Split into per-traceback runs. A "run" is everything between two
        # ``_TRACEBACK_START`` markers (or from the first start marker to
        # the next exception summary that closes a traceback). Empty start
        # markers are handled by allowing the first run to begin at the
        # first File-frame we encounter, even without an explicit start.
        runs = self._split_runs(lines)
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

    def _split_runs(self, lines: Sequence[Line]) -> list[tuple[int, int]]:
        """Return ``[(lo, hi), ...]`` line-index spans of each traceback."""
        starts: list[int] = []
        for i, ln in enumerate(lines):
            if ln.text and _TRACEBACK_START.match(ln.text):
                starts.append(i)
            elif not starts and _FRAME.match(ln.raw):
                # Traceback whose start banner got stripped (e.g. by a
                # logging adapter that dropped the first line) — start an
                # implicit run at the first frame.
                starts.append(i)

        if not starts:
            return []

        starts.append(len(lines))
        return [(starts[k], starts[k + 1]) for k in range(len(starts) - 1)]

    def _contribute_run(
        self,
        lines: Sequence[Line],
        run: tuple[int, int],
        *,
        score_deltas: dict[int, int],
        keep_marks: set[int],
        notes: dict[int, str],
        locations: list[FailureLocation],
    ) -> None:
        lo, hi = run

        # Collect frames in order. We also remember the index of the
        # *next non-frame line* after each frame so the body snippet (the
        # offending source line Python prints under each File entry) can
        # be annotated as a note.
        frames: list[tuple[int, re.Match[str]]] = []
        for i in range(lo, hi):
            m = _FRAME.match(lines[i].raw)
            if m is not None:
                frames.append((i, m))

        if not frames:
            return

        # Exception summary: scan the tail of the run for a class-like
        # line. Cap the lookback so a giant stderr dump doesn't poison
        # the message field.
        exc_cls = ""
        exc_msg = ""
        exc_log_idx = -1
        for k in range(hi - 1, max(frames[-1][0], hi - 6), -1):
            ln = lines[k]
            if not ln.text:
                continue
            em = _EXCEPTION_SUMMARY.match(ln.text)
            if em:
                exc_cls = em.group("cls")
                exc_msg = (em.group("msg") or "").strip()[:160]
                exc_log_idx = ln.idx
                score_deltas[k] = score_deltas.get(k, 0) + 2
                keep_marks.add(k)
                break

        # Bottom-most project frame is the bug site in Python.
        bottom_project_pos: int | None = None
        for pos in range(len(frames) - 1, -1, -1):
            i, m = frames[pos]
            if not _is_framework_file(m.group("file")):
                bottom_project_pos = pos
                break

        for pos, (i, m) in enumerate(frames):
            file = m.group("file")
            line_no = int(m.group("line"))
            function = m.group("function") or ""
            is_project = not _is_framework_file(file)

            if not is_project:
                continue

            depth_from_bottom = len(frames) - 1 - pos
            confidence = self._confidence_for(
                depth_from_bottom=depth_from_bottom,
                is_bottom_project=(pos == bottom_project_pos),
            )

            loc = FailureLocation(
                kind="source",
                file=file,
                line=line_no,
                function=function,
                detector=self.name,
                confidence=confidence,
                log_line_idx=lines[i].idx,
                message=(
                    f"{exc_cls}: {exc_msg}".rstrip(": ")
                    if exc_cls
                    else (function or file)
                ),
            )
            locations.append(loc)

            # Score boost: the bug site gets the highest boost so the
            # anchor cluster centers there.
            score_deltas[i] = score_deltas.get(i, 0) + (
                3 if pos == bottom_project_pos else 2
            )
            keep_marks.add(i)

            # Annotate the bug-site frame with the exception class so the
            # LLM body shows the smoking gun without scrolling back up.
            if pos == bottom_project_pos and exc_cls and not notes.get(i):
                tail = f": {exc_msg}" if exc_msg else ""
                notes[i] = f"<-- {exc_cls}{tail}"

            # If the next line is the body-source snippet Python prints
            # under each File entry, keep it too (one line of context that
            # often makes the bug obvious).
            if i + 1 < hi:
                next_text = lines[i + 1].text
                if next_text and not _FRAME.match(lines[i + 1].raw):
                    keep_marks.add(i + 1)
                    score_deltas[i + 1] = score_deltas.get(i + 1, 0) + 1

        # No project frame? Fall back to the deepest (bottom) frame at a
        # lower confidence so the locator still has something concrete.
        if bottom_project_pos is None and frames:
            i, m = frames[-1]
            locations.append(
                FailureLocation(
                    kind="source",
                    file=m.group("file"),
                    line=int(m.group("line")),
                    function=m.group("function") or "",
                    detector=self.name,
                    confidence=0.45,
                    log_line_idx=lines[i].idx,
                    message=exc_cls or "framework-internal traceback",
                )
            )
            score_deltas[i] = score_deltas.get(i, 0) + 1

    def _confidence_for(
        self, *, depth_from_bottom: int, is_bottom_project: bool
    ) -> float:
        if is_bottom_project:
            return 0.85
        # Frames above the bottom project frame are still informative but
        # rank below the bug site.
        return max(0.55, 0.85 - 0.10 * (depth_from_bottom + 1))


_INSTANCE = PythonTracebackDetector()
register(_INSTANCE)
