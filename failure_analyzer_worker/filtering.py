"""Pure log cleaner + fingerprint extractor (no I/O, no vendor dependencies).

The ``jenkins_failure_listener`` already crops a tight window around the
real error anchors; this module's job is to *clean* that excerpt
(timestamps, blank runs, Jenkins/Maven/Spring/Docker boilerplate) and feed
it to the LLM as-is. All retrieval / embedding concerns live in
``vectorstore/`` and ``embeddings/``.

This module is importable without torch, elasticsearch, or any LangChain
package — the filter / fingerprint pipeline is pure Python so CI tests can
exercise it with zero network or heavy-dependency cost.
"""

from __future__ import annotations

import re

from .config import settings

# ── Noise patterns ─────────────────────────────────────────────────────────────

_TIMESTAMP_STRIP = re.compile(
    r"^(?:"
    # ISO-8601: 2026-04-06T05:59:53.123Z  or  [2026-04-06T05:59:53.123Z]
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

# Lines that carry no diagnostic value -- drop them outright.
_NOISE_LINE = re.compile(
    r"""(?ix)
    ^\s*$ |

    # Jenkins pipeline machinery
    ^\s*\[Pipeline\]\s* |
    ^\s*(?:Started\s+by|Running\s+in|Established\s+SSH)\s |
    ^\s*(?:Cloning\s+repository|Checking\s+out\s+Revision|using\s+credential) |
    ^\s*>\s*git\s | ^\s*Fetching\s+upstream |
    ^\s*Commit\s+message: | ^\s*First\s+time\s+build |

    # Maven / Gradle [INFO] progress / banners (no diagnostic value)
    \[INFO\]\s*[-=]{4,}\s*$ |
    \[INFO\]\s*$ |
    \[INFO\]\s*(?:Scanning\s+for\s+projects|Building\s|Compiling\s|Copying\s|
        Installing\s|Deleting\s|Recompiling\s|Downloaded\s|Downloading\s|
        Progress\s|Resolving\s|---\s|Total\s+time|Finished\s+at|
        BUILD\s+SUCCESS|No\s+sources\s+to\s+compile|Nothing\s+to\s+compile|
        Changes\s+detected|Generating\s) |
    \[WARNING\]\s*(?:'dependencies|Some\s+problems\s+were|It\s+is\s+highly\s+recommended|
        \s*from\s+pom\.xml) |

    # Package / dependency downloads (Maven, Gradle, sbt, etc.)
    ^\s*(?:Downloading|Downloaded|Uploading|Uploaded)\s+(?:from|to)\s |
    ^\s*Progress\s*\(\d+\) |

    # Docker pull progress
    ^\s*[0-9a-f]{6,}:\s*(?:Pulling|Waiting|Downloading|Extracting|
        Pull\s+complete|Already\s+exists|Verifying|Layer\s+already|Digest:|Status:) |

    # npm / yarn noise
    ^\s*(?:npm\s+(?:warn|notice)|added\s+\d+\s+packages|up\s+to\s+date|
        yarn\s+install|Resolving\s+packages|Fetching\s+packages|
        Linking\s+dependencies|Building\s+fresh\s+packages) |

    # Generic shell echo / set-x lines
    ^\s*\+\s*(?:echo|export|cd|mkdir|chmod|set)\s |

    # Spring Boot banner / version line
    ^\s*::\s*Spring\s+Boot\s*:: |
    ^\s*\(v\d+\.\d+\.\d+\)\s*$ |

    # Surefire / Mockito harness chatter
    ^\[INFO\]\s+(?:Running\s+com\.|Surefire\s+report\s+directory:|
        Using\s+auto\s+detected\s+provider|T\s+E\s+S\s+T\s+S)\b |
    ^\s*Mockito\s+is\s+currently\s+self-attaching |
    ^\s*WARNING:\s+(?:A\s+(?:Java\s+agent|terminally\s+deprecated\s+method)|
        If\s+a\s+serviceability\s+tool|Please\s+consider\s+reporting)
    """,
)

_HORIZONTAL_WS = re.compile(r"[^\S\n]+")
_REPEATED_MARKER = " (repeated)"


# ── The filter ─────────────────────────────────────────────────────────────────


def filter_logs(raw_text: str) -> str:
    """Strip timestamps, drop noise lines, collapse whitespace, dedupe consecutive
    duplicates, cap to ``LOG_BODY_MAX_CHARS`` keeping the tail.

    No scoring, no bucketing, no anchor windows -- the listener already
    narrowed the log to the relevant region.
    """
    if not raw_text:
        return ""

    text = raw_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")

    out: list[str] = []
    last_kept: str | None = None
    prev_blank = False

    for raw in text.split("\n"):
        line = _TIMESTAMP_STRIP.sub("", raw)
        line = _HORIZONTAL_WS.sub(" ", line).strip()

        if not line:
            if not prev_blank and out:
                out.append("")
                prev_blank = True
            continue

        if _NOISE_LINE.match(line):
            continue

        if line == last_kept:
            if out and not out[-1].endswith(_REPEATED_MARKER):
                out[-1] = out[-1] + _REPEATED_MARKER
            continue

        out.append(line)
        last_kept = line
        prev_blank = False

    body = "\n".join(out).strip()

    cap = settings.log_body_max_chars
    if len(body) > cap:
        tail = body[-cap:]
        nl = tail.find("\n")
        if 0 < nl < len(tail) - 200:
            tail = tail[nl + 1 :]
        body = "... [head truncated]\n" + tail

    return body


# ── Error-signature helpers (used for fingerprinting + UI metadata) ───────────

_EXCEPTION_LINE = re.compile(r"\b((?:[\w$]+\.)*[\w$]+(?:Exception|Error|Failure|Fault))\b")
_CAUSED_BY = re.compile(r"(?i)Caused\s+by:\s*(.+)")
_ERROR_MARKER = re.compile(r"(?:^|\s)(?:ERROR|FATAL|SEVERE)\b\s*[:\-]?\s*(.+)")
_EXIT_CODE = re.compile(
    r"(?i)(?:exit\s+code|returned\s+exit\s+code|process\s+exited\s+with)"
    r"\s*[:=]?\s*(\d+)",
)


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def primary_error(filtered: str) -> str:
    """Caused-by → Exception class → ERROR/FATAL/SEVERE line → 'Unknown'."""
    return (
        _first(_CAUSED_BY, filtered)
        or _first(_EXCEPTION_LINE, filtered)
        or _first(_ERROR_MARKER, filtered)
        or "Unknown"
    )


# ── Fingerprint for vector-store retrieval (focused, low-noise) ───────────────


def generate_fingerprint(filtered: str, stage_name: str) -> str:
    """Caused-by → first 4 exception types → first ERROR line → stage.

    Deliberately omits generic phrases (``not found`` / ``timed out`` /
    ``refused``) that previously polluted embeddings and produced weak
    retrieval neighbours.
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


# ── LogProcessor: filtered body + small metadata header (UI only) ─────────────


class LogProcessor:
    """Thin wrapper that prepends a metadata header to filtered output.

    The header is for the UI only; the LLM should rely on the body, not the
    header (the header's heuristics are best-effort).
    """

    def format_with_metadata(self, raw: str, body: str) -> str:
        primary = primary_error(body)
        code = _first(_EXIT_CODE, raw + "\n" + body) or "N/A"
        ratio = 0.0 if not raw else round(100.0 * (1.0 - len(body) / len(raw)), 1)
        header = (
            "---\n"
            "[METADATA]\n"
            f"- Primary Error: {primary}\n"
            f"- Exit Code: {code}\n"
            f"- Compression: {len(raw)} → {len(body)} chars (~{ratio}% removed)\n"
            "---\n"
        )
        return header + body + "\n---"

    def process(self, raw: str) -> str:
        return self.format_with_metadata(raw, filter_logs(raw))
