"""Stack-agnostic detector for shell-style failures.

Activates on every log (priority 0) because every CI step ultimately ends
with some shell command's exit code. Contributes one :class:`FailureLocation`
pointing at the **last command line preceding an exit/error marker** — a
universal pattern across Maven, Gradle, Make, npm scripts, plain shell, etc.

This detector is the canonical example of the :class:`Detector` protocol:
no framework knowledge, no stack-specific regex, just structural reasoning
on already-classified ``Line`` records. New detectors should aim for the
same minimalism — anything that recognizes the *shape* of a failure rather
than the *vocabulary* of a specific tool.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..base import Contribution, FailureLocation, Kind, Line, Severity
from . import register


_EXIT_NONZERO = re.compile(
    r"(?i)(?:exit|return)\s*(?:code|status)?\s*[:=]?\s*([1-9]\d*)\b|"
    r"script\s+returned\s+exit\s+code\s*([1-9]\d*)|"
    r"process\s+exited\s+with\s+(?:code\s+)?([1-9]\d*)"
)


class GenericShellDetector:
    """Stack-agnostic floor detector."""

    name = "generic_shell"
    priority = 0  # ground-floor; any stack-specific detector outranks it

    def activates_on(self, probe: str) -> bool:
        # The cheap probe always returns True — this detector is the
        # universal floor. The real validation is in contribute(), which
        # returns None if the log actually has no exit code or no command
        # context to point at.
        return True

    def contribute(self, lines: Sequence[Line]) -> Contribution | None:
        # Find the *last* line carrying a non-zero exit/return marker.
        exit_idx = -1
        exit_code = 0
        for i in range(len(lines) - 1, -1, -1):
            ln = lines[i]
            if ln.drop or not ln.text:
                continue
            m = _EXIT_NONZERO.search(ln.text)
            if m:
                exit_idx = i
                try:
                    exit_code = int(next(g for g in m.groups() if g))
                except (StopIteration, ValueError):
                    exit_code = 1
                break

        if exit_idx < 0:
            # No exit marker → nothing this detector can localize.
            # Other detectors (or the locator's log-line fallback) handle
            # the case.
            return None

        # Walk back from the exit marker to find the last CMD line.
        cmd_idx = -1
        for k in range(exit_idx - 1, max(-1, exit_idx - 60), -1):
            if lines[k].drop:
                continue
            if lines[k].kind == Kind.CMD:
                cmd_idx = k
                break
            # Stop early if we crossed into a stack trace — the exit is
            # likely tied to that throwing line, not a preceding command.
            if lines[k].kind == Kind.STACK or lines[k].severity >= Severity.CAUSE:
                break

        score_deltas: dict[int, int] = {exit_idx: 2}
        keep_marks: set[int] = {exit_idx}
        locations: list[FailureLocation] = []

        if cmd_idx >= 0:
            cmd_text = lines[cmd_idx].text.lstrip("+$> ").strip()
            score_deltas[cmd_idx] = 2
            keep_marks.add(cmd_idx)
            locations.append(
                FailureLocation(
                    kind="command",
                    command=cmd_text[:200],
                    log_line_idx=lines[cmd_idx].idx,
                    detector=self.name,
                    confidence=0.55 if cmd_text else 0.40,
                    message=f"exited with code {exit_code}",
                )
            )
        else:
            # Exit marker without an attached command — still localizable
            # to the log line, but lower confidence.
            locations.append(
                FailureLocation(
                    kind="log",
                    log_line_idx=lines[exit_idx].idx,
                    detector=self.name,
                    confidence=0.30,
                    message=f"non-zero exit ({exit_code})",
                )
            )

        return Contribution(
            detector=self.name,
            score_deltas=score_deltas,
            keep_marks=frozenset(keep_marks),
            locations=tuple(locations),
        )


_INSTANCE = GenericShellDetector()
register(_INSTANCE)
