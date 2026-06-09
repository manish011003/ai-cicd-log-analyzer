"""Detector — Java / Kotlin / Scala stack traces.

Activates on any log carrying at least one ``\\s+at <fqcn>(<file>:<line>)``
frame. Emits one :class:`FailureLocation` per *project* frame, with the
topmost project frame getting the highest confidence (in Java, the top of
the stack is the deepest call — that's where the error originated, so the
first project frame encountered walking top-to-bottom is the bug site).

What this detector deliberately does **not** do:

* It does not own structural collapse — that's the universal Pass 2's job.
  This module only reads the already-classified ``Line.kind == STACK``
  records and the parsed frame metadata.
* It does not maintain a list of "official" Java frameworks — only widely
  shared infrastructure (``java.*``, ``sun.*``, ``jdk.internal.*``, JPMS
  module prefixes, and a small set of broadly-deployed third-party
  packages). Anything else is treated as project code. If you genuinely
  want a deeper framework filter, write a project-specific detector.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..base import Contribution, FailureLocation, Line
from . import register


# ── Activation probe ───────────────────────────────────────────────────────

_PROBE = re.compile(r"^\s+at\s+[\w$./<>]+\([^)]*\)", re.MULTILINE)


# ── Frame parsing ─────────────────────────────────────────────────────────
#
# Capture FQCN, file, optional line. The file may be ``Native Method``
# (no file), ``Unknown Source`` (no info), or a real ``Foo.java`` name.
# JPMS frames carry a module prefix joined by ``/`` — we strip it for the
# function display but keep it in the raw FQCN for de-duplication.

_FRAME = re.compile(
    r"^\s+at\s+"
    r"(?:(?P<module>[\w$.]+)/)?"
    r"(?P<fqcn>[\w$.<>]+)"
    r"\((?P<details>[^)]*)\)"
)
_FRAME_FILE_LINE = re.compile(r"^(?P<file>[^:]+?)(?::(?P<line>\d+))?$")

_CAUSE_LINE = re.compile(
    r"(?:^|\s)(?P<cls>(?:[\w$]+\.)*[\w$]+(?:Exception|Error|Failure|Fault))(?::|$)"
)


# ── Framework heuristics ──────────────────────────────────────────────────
#
# Conservative list — only widely-shared infrastructure that no one ever
# wrote themselves. Everything else is treated as project code, which is
# the safe default (a "third-party but on the failure path" frame is still
# more actionable than a JDK-internal frame).

_FRAMEWORK_PREFIXES: tuple[str, ...] = (
    # JDK & language runtimes
    "java.",
    "javax.",
    "jakarta.",
    "kotlin.",
    "kotlinx.",
    "scala.",
    "groovy.",
    "sun.",
    "com.sun.",
    "jdk.internal.",
    # Test frameworks
    "org.junit.",
    "junit.",
    "org.testng.",
    "org.mockito.",
    "org.assertj.",
    "org.hamcrest.",
    "io.cucumber.",
    # Application frameworks
    "org.springframework.",
    "org.springframework.boot.",
    "org.hibernate.",
    "org.eclipse.jetty.",
    "org.glassfish.",
    "org.apache.tomcat.",
    "org.apache.catalina.",
    "org.apache.coyote.",
    # Build tools
    "org.apache.maven.",
    "org.apache.tools.ant.",
    "org.gradle.",
    # Logging
    "ch.qos.logback.",
    "org.slf4j.",
    "org.apache.log4j.",
    "org.apache.logging.log4j.",
    # Widely-deployed third-party libraries (almost never the bug site;
    # bumping these up the framework list moves the locator one frame
    # closer to the user's actual code).
    "org.apache.http.",
    "org.apache.commons.",
    "com.google.common.",
    "com.google.inject.",
    "com.google.gson.",
    "com.fasterxml.jackson.",
    "io.netty.",
    "io.micrometer.",
    "io.reactivex.",
    "reactor.core.",
    "okhttp3.",
    "retrofit2.",
    "com.zaxxer.hikari.",
    "feign.",
)

_FRAMEWORK_MODULES: tuple[str, ...] = (
    "java.base",
    "java.desktop",
    "java.logging",
    "java.management",
    "java.naming",
    "java.net.http",
    "java.security.jgss",
    "java.sql",
    "java.xml",
    "jdk.compiler",
    "jdk.internal",
    "jdk.proxy1",
    "jdk.proxy2",
    "jdk.unsupported",
)


def _is_framework(module: str | None, fqcn: str) -> bool:
    if module and module in _FRAMEWORK_MODULES:
        return True
    if module and module.startswith("java.") and not module.startswith("java.user."):
        return True
    return any(fqcn.startswith(p) for p in _FRAMEWORK_PREFIXES)


# ── Helpers ───────────────────────────────────────────────────────────────


def _split_method(fqcn: str) -> tuple[str, str]:
    """``com.acme.Foo$Inner.bar`` → ``("com.acme.Foo$Inner", "bar")``."""
    dot = fqcn.rfind(".")
    if dot < 0:
        return ("", fqcn)
    return (fqcn[:dot], fqcn[dot + 1 :])


def _parse_details(details: str) -> tuple[str, int]:
    """``Foo.java:42`` → ``("Foo.java", 42)``. Returns line=0 if unknown."""
    if not details or details in ("Native Method", "Unknown Source"):
        return ("", 0)
    m = _FRAME_FILE_LINE.match(details.strip())
    if not m:
        return ("", 0)
    line = int(m.group("line")) if m.group("line") else 0
    return (m.group("file"), line)


# ── Detector ───────────────────────────────────────────────────────────────


class JavaStackDetector:
    """Localizes Java/Kotlin/Scala failures to a project frame."""

    name = "java_stack"
    priority = 50  # outranks the generic_shell floor (priority 0)

    def activates_on(self, probe: str) -> bool:
        return _PROBE.search(probe) is not None

    def contribute(self, lines: Sequence[Line]) -> Contribution | None:
        # Identify contiguous Java-frame runs. We re-scan the raw line text
        # rather than trust ``kind == STACK`` alone — that way a stack run
        # whose frames were partially collapsed by Pass 2 still surfaces
        # every locatable frame (collapsed frames carry ``drop=True`` but
        # the original raw text is still available on the Line record).
        stacks: list[list[tuple[int, re.Match[str]]]] = []
        current: list[tuple[int, re.Match[str]]] = []
        for i, ln in enumerate(lines):
            m = _FRAME.match(ln.raw)
            if m is not None:
                current.append((i, m))
                continue
            if current:
                stacks.append(current)
                current = []
        if current:
            stacks.append(current)

        if not stacks:
            return None

        score_deltas: dict[int, int] = {}
        keep_marks: set[int] = set()
        notes: dict[int, str] = {}
        locations: list[FailureLocation] = []

        for stack in stacks:
            self._contribute_stack(
                lines,
                stack,
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

    def _contribute_stack(
        self,
        lines: Sequence[Line],
        stack: list[tuple[int, re.Match[str]]],
        *,
        score_deltas: dict[int, int],
        keep_marks: set[int],
        notes: dict[int, str],
        locations: list[FailureLocation],
    ) -> None:
        top_idx, _ = stack[0]

        # Find the throwing line: the closest non-stack line above the run
        # that mentions an exception class. ``Caused by: <FQCN>`` wins,
        # else any ``\*Exception``/``\*Error``-ending class.
        cause_cls = ""
        cause_msg = ""
        cause_log_idx = -1
        for k in range(top_idx - 1, max(-1, top_idx - 8), -1):
            ln = lines[k]
            if not ln.text:
                continue
            cm = _CAUSE_LINE.search(ln.text)
            if cm:
                cause_cls = cm.group("cls")
                # Best-effort message: anything after the colon on the
                # same line; trimmed for the metadata anchor.
                colon = ln.text.find(":", cm.end())
                if colon > 0:
                    cause_msg = ln.text[colon + 1 :].strip()[:120]
                cause_log_idx = ln.idx
                score_deltas[k] = score_deltas.get(k, 0) + 2
                keep_marks.add(k)
                break

        # Walk the stack top-to-bottom; first project frame is the bug site.
        # ``project_offset`` counts *project-only* depth so the confidence
        # decay isn't dominated by however many framework frames the JDK or
        # Spring stuck on top of the real call site.
        top_project_idx = -1
        project_offset = 0
        for i, m in stack:
            module = m.group("module")
            fqcn = m.group("fqcn")
            details = m.group("details")
            file, line = _parse_details(details)
            cls, method = _split_method(fqcn)

            if _is_framework(module, fqcn):
                continue

            confidence = self._confidence_for(project_offset, file, line)
            project_offset += 1
            class_display = f"{module}/{cls}" if module else cls

            loc = FailureLocation(
                kind="source",
                file=file,
                line=line,
                function=f"{class_display}.{method}" if class_display else method,
                detector=self.name,
                confidence=confidence,
                log_line_idx=lines[i].idx,
                message=(
                    f"{cause_cls}: {cause_msg}".rstrip(": ")
                    if cause_cls
                    else (method or fqcn)
                ),
            )
            locations.append(loc)

            score_deltas[i] = score_deltas.get(i, 0) + (3 if top_project_idx < 0 else 2)
            keep_marks.add(i)

            if top_project_idx < 0:
                top_project_idx = i
                if cause_cls and not notes.get(i):
                    notes[i] = f"<-- {cause_cls}"

        # If the stack had no project frames at all (pure framework
        # internals), still emit ONE location pointing at the topmost
        # frame so the locator has something to anchor on — at lower
        # confidence, since the bug is somewhere upstream.
        if top_project_idx < 0 and stack:
            i, m = stack[0]
            module = m.group("module")
            fqcn = m.group("fqcn")
            file, line = _parse_details(m.group("details"))
            cls, method = _split_method(fqcn)
            locations.append(
                FailureLocation(
                    kind="source",
                    file=file,
                    line=line,
                    function=(f"{module}/{cls}" if module else cls) + f".{method}",
                    detector=self.name,
                    confidence=0.45,
                    log_line_idx=lines[i].idx,
                    message=cause_cls or "framework-internal stack",
                )
            )
            score_deltas[i] = score_deltas.get(i, 0) + 1

    def _confidence_for(self, offset: int, file: str, line: int) -> float:
        # Topmost project frame → 0.85; subsequent project frames decay
        # slowly. Frames missing file:line info are capped because the
        # locator can't deep-link them in the UI.
        base = 0.85 if offset == 0 else max(0.55, 0.85 - 0.10 * offset)
        if not file:
            return min(base, 0.50)
        if line <= 0:
            return min(base, 0.65)
        return base


_INSTANCE = JavaStackDetector()
register(_INSTANCE)
