"""Detector — Node.js V8 stack traces.

Built on :class:`GenericStackDetector` rather than a hand-written class
because the Node convention is structurally simple:

* Frame shape: ``    at <fn> (<file>:<line>:<col>)``, with optional
  ``async`` prefix and anonymous-callback variants.
* Bug site is at the **top** of the stack (deepest call shown first,
  same as Java).
* Framework code is anything under ``node_modules`` or the built-in
  ``node:``/``internal/`` prefixes.

A future Layer 2 (declarative YAML) loader will replace this module with
a ``node_stack.yaml`` spec that hydrates the same :class:`StackLanguage`.
Until then, the Python config keeps things diff-friendly and avoids the
need for a YAML loader / regex-sandbox dependency.
"""

from __future__ import annotations

from . import register
from .generic_stack import GenericStackDetector, StackLanguage


_NODE = StackLanguage(
    name="node_stack",
    priority=50,
    description="Localizes Node.js failures to the topmost project frame.",
    # Probe — keep cheap; just needs to spot one V8 frame in the probe window.
    probe_pattern=r"^\s+at\s+(?:async\s+)?\S+\s*\([^)]+:\d+:\d+\)",
    # Frame — captures file/line/col always; function name when present.
    # The non-capturing ``async `` accommodates async stack traces. The
    # alternation handles the two V8 frame shapes:
    #   "    at Object.handler (/app/src/handlers/payment.js:42:15)"
    #   "    at /app/src/server.js:128:5"     (anonymous)
    frame_pattern=(
        r"^\s+at\s+"
        r"(?:(?:async\s+)?(?P<function>[\w$.<>\[\] ]+)\s+)?"  # optional fn name
        r"\(?(?P<file>[^():\n]+):(?P<line>\d+):(?P<column>\d+)\)?\s*$"
    ),
    # Loose "looks like a V8 frame" pattern. Keeps the run alive across
    # decorative frames like ``    at async Promise.all (index 0)`` that
    # don't carry a file:line — otherwise each project frame would land
    # in its own 1-frame run and the bug-site confidence-decay would
    # collapse, letting the locator's tie-break pick the wrong frame.
    run_separator_pattern=r"^\s+at\s+\S",
    framework_paths=(
        "/node_modules/",
        "\\node_modules\\",
        "node:internal",
        "node:fs",
        "node:events",
        "node:async_hooks",
        "internal/process/",
        "internal/modules/",
        "internal/main/",
    ),
    bug_site="top",
    # Node prints the error class on the line *above* the stack:
    #   "TypeError: Cannot read property 'foo' of undefined"
    cause_pattern=r"^(?P<cls>(?:[\w$.]+\.)*[A-Z]\w*(?:Error|Exception))(?::\s*(?P<msg>.*))?$",
    cause_position="above",
    cause_lookback=6,
)


register(GenericStackDetector(_NODE))
