"""Log filtering, fingerprinting, and Elasticsearch (embeddings + knn + store)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer

from .config import settings

logger = logging.getLogger(__name__)

_TIMESTAMP_STRIP = re.compile(
    r"^(?:"
    # ISO-8601  2026-04-06T05:59:53.123Z  or  [2026-04-06T05:59:53.123Z]
    r"\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\]?\s*"
    r"|"
    # bare wall-clock  14:30:22.456
    r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+"
    r"|"
    # human date  Apr 6, 2026 5:59:53 AM
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*(?:AM|PM)?\s*"
    r")"
)

_NOISE_LINE = re.compile(
    r"(?i)"
    # blank
    r"^\s*$|"

    # [Pipeline] boilerplate & flow markers
    r"^\s*\[Pipeline\]\s*(?:\{|\}|//\s*\w+|$)|"
    r"^\s*\[Pipeline\]\s*(?:stage|node|parallel|withEnv|timestamps|timeout|"
    r"getContext|End of Pipeline|echo|sh|bat|script|wrap|tool|readFile|"
    r"isUnix|dir|archiveArtifacts|junit|stash|unstash)\b|"

    # Maven / Gradle [INFO] banners & progress
    r"^\s*(?:\[?\d{4}[^\]]*\]?\s*)?\[INFO\]\s*[-=]{4,}\s*$|"
    r"^\s*(?:\[?\d{4}[^\]]*\]?\s*)?\[INFO\]\s*$|"
    r"^\s*(?:\[?\d{4}[^\]]*\]?\s*)?\[INFO\]\s*(?:Scanning for projects|"
    r"Building\s|Compiling\s|Copying\s|Installing\s|Deleting\s|Recompiling\s|"
    r"--- \S+:\S+:\S+ .* ---\s*$|BUILD SUCCESS|Total time:|Finished at:|"
    r"No sources to compile|Nothing to compile|skip non existing|"
    r"Changes detected|Generating\s)|"

    # Maven [WARNING] pom / dependency model noise
    r"^\s*(?:\[?\d{4}[^\]]*\]?\s*)?\[WARNING\]\s*(?:'dependencies|"
    r"Some problems were|It is highly recommended|\s*from pom\.xml)|"

    # Package download & dependency resolution
    r"^\s*(?:Downloading|Downloaded|Uploading|Uploaded)\s+(?:from|to)\s|"
    r"^\s*(?:\[?\d{4}[^\]]*\]?\s*)?\[INFO\]\s*(?:Downloaded\s|Downloading\s|"
    r"Progress\s|Resolving\s)|"
    r"^\s*Progress\s*\(\d+\)|"

    # Docker pull progress
    r"^\s*[0-9a-f]+:\s*(?:Pulling|Waiting|Downloading|Extracting|"
    r"Pull complete|Already exists|Verifying|Layer already|Digest:|Status:)|"

    # Git operations
    r"^\s*(?:Cloning repository|Checking out Revision|using credential|"
    r">\s*git\s|Fetching upstream|Commit message:|First time build)|"

    # npm / yarn noise
    r"^\s*(?:npm warn|npm notice|added \d+ packages|up to date|"
    r"yarn install|Resolving packages|Fetching packages|"
    r"Linking dependencies|Building fresh packages)|"

    # Generic CI markers with no diagnostic value
    r"^\s*(?:Started by|Running in|Established SSH|"
    r"\+\s*(?:echo|export|cd|mkdir|chmod|set)\s)|"

    # Spring Boot banner / version line
    r"^\s*::\s*Spring Boot ::|"
    r"^\s*\(v\d+\.\d+\.\d+\)\s*$|"
    # Spring Cloud config-client retry spam (after timestamp strip: "INFO 4893 --- ...")
    r"^\s*INFO\s+\d+\s+---\s+\[[^\]]+\]\s+.*ConfigServerConfigDataLoader\s*:|"
    r"^\s*INFO\s+\d+\s+---\s+\[[^\]]+\]\s+.*\s+Fetching config from server at\s*:|"
    r"^\s*INFO\s+\d+\s+---\s+\[[^\]]+\]\s+.*Exception on Url\s*-|"
    # Java stack frames / trace filler (some tools strip leading whitespace)
    r"^\s*at\s+(?:[\w.$]+\.)+[\w$]+\([^)]*\)\s*(?:~\[.*)?\s*$|"
    r"^\s*\.{3}\s+\d+\s+more\s*$|"
    r"^\s*Caused by:\s*$|"
    # Maven Surefire / Mockito harness
    r"^\[INFO\]\s+(?:Running com\.|Surefire report directory:|Using auto detected provider|T E S T S)\b|"
    r"^\s*Mockito is currently self-attaching|"
    r"^\s*WARNING:\s+A (?:Java agent|terminally deprecated method)|"
    r"^\s*WARNING:\s+If a serviceability tool|"
    r"^\s*WARNING:\s+Please consider reporting|"
    r"^.*Spring Cloud LoadBalancer is currently working with the default cache"
)

_EXCEPTION_CLASS = re.compile(
    r"\b([a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*"
    r"(?:Exception|Error|Failure|Fault))\b"
)
_CAUSED_BY = re.compile(
    r"Caused\s+by:\s*(\S+(?:Exception|Error|Failure|Fault)\b[^\n]*)",
    re.IGNORECASE,
)
_ERROR_LINE = re.compile(
    r"(?:^|\s)(?:ERROR|FATAL|SEVERE)\s+(.+)", re.IGNORECASE
)
_KEY_SIGNAL = re.compile(
    r"(?i)\b(?:"
    r"connection\s+refused|connection\s+reset|connection\s+timed?\s*out|"
    r"no\s+such\s+file|permission\s+denied|access\s+denied|"
    r"out\s+of\s+memory|killed|segfault|segmentation\s+fault|"
    r"stack\s*overflow|"
    r"could\s+not\s+(?:find|resolve|connect|create|open|read|write|execute)|"
    r"cannot\s+(?:find|resolve|connect|create|open|read|write|execute)|"
    r"unable\s+to\s+(?:find|resolve|connect|create|open|read|write|execute)|"
    r"failed\s+to\s+(?:start|stop|connect|build|compile|deploy|load|initialize)|"
    r"not\s+found|timed?\s*out|refused|"
    r"exit\s+code\s*[:=]?\s*[1-9]\d*|"
    r"non-zero\s+exit|returned\s+exit\s+code|"
    r"assert(?:ion)?(?:\s+(?:failed|error))?"
    r")\b"
)

# Spring Boot log line: --- [service-name] [thread] ...
_SPRING_SERVICE_BUCKET = re.compile(r"---\s+\[([^\]]+)\]\s+\[")
_STACK_CONTINUATION = re.compile(
    r"^\s*(?:at\s+(?:[\w$]+\.)+[\w$]+\(|Caused\s+by:\s*\S|\.{3}\s+\d+\s+more\b)"
)
_URL_FOR_TEMPLATE = re.compile(r"https?://[^\s)>\]]+")
_HEX_FOR_TEMPLATE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM_FOR_TEMPLATE = re.compile(r"\d+")
_JSON_LOG_KEYS = (
    "raw_logs",
    "log_excerpt",
    "filtered_logs",
    "message",
    "log",
    "excerpt",
    "stdout",
    "stderr",
    "console_output",
    "output",
)
_CI_FAILURE_MARKERS = re.compile(
    r"(?i)(BUILD\s+FAILURE|<<<\s*FAILURE!|Tests\s+run:\s*[^,\n]+,\s*Failures:\s*[1-9]|"
    r"FAILURE\s*\[|There\s+are\s+test\s+failures|ERROR\s+Tests\s+failed)"
)


def _extract_log_string_from_json(obj: Any, depth: int = 0) -> str | None:
    if depth > 6:
        return None
    if isinstance(obj, str) and len(obj.strip()) > 80:
        return obj
    if isinstance(obj, dict):
        for k in _JSON_LOG_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and len(v.strip()) > 40:
                return v
        for v in obj.values():
            found = _extract_log_string_from_json(v, depth + 1)
            if found:
                return found
    if isinstance(obj, list):
        for item in obj[:30]:
            found = _extract_log_string_from_json(item, depth + 1)
            if found:
                return found
    return None


def _jsonish_unescape(s: str) -> str:
    """Decode common JSON-style escapes without treating the whole blob as Python unicode_escape."""
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "\\" and i + 1 < n:
            c = s[i + 1]
            if c == "n":
                out.append("\n")
                i += 2
                continue
            if c == "r":
                out.append("\r")
                i += 2
                continue
            if c == "t":
                out.append("\t")
                i += 2
                continue
            if c == '"':
                out.append('"')
                i += 2
                continue
            if c == "\\":
                out.append("\\")
                i += 2
                continue
            if c == "/":
                out.append("/")
                i += 2
                continue
            if c == "u" and i + 5 < n:
                hx = s[i + 2 : i + 6]
                if len(hx) == 4 and all(ch in "0123456789abcdefABCDEF" for ch in hx):
                    try:
                        out.append(chr(int(hx, 16)))
                        i += 6
                        continue
                    except ValueError:
                        pass
        out.append(s[i])
        i += 1
    return "".join(out)


def normalize_log_text(raw: str) -> str:
    """Stage 1: line endings, optional JSON unwrap, conservative escape decoding."""
    if not raw:
        return ""
    s = raw.replace("\r\n", "\n").replace("\r", "\n")
    s = s.removeprefix("\ufeff")
    stripped = s.strip()

    if stripped.startswith("{"):
        try:
            obj = json.loads(s)
            inner = _extract_log_string_from_json(obj)
            if inner is not None:
                s = inner
        except json.JSONDecodeError:
            pass
    elif stripped.startswith('"'):
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, str):
                s = decoded
        except json.JSONDecodeError:
            pass

    literal_bn = s.count("\\n")
    lines = s.splitlines()
    long_blob = len(s) > 800
    few_lines = len(lines) <= 4
    if few_lines and long_blob and (literal_bn >= 5 or (len(s) > 3000 and literal_bn >= 2)):
        s2 = _jsonish_unescape(s)
        if s2.count("\n") > len(lines) * 2:
            s = s2
            t = s.strip()
            if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
                try:
                    inner2 = json.loads(t)
                    if isinstance(inner2, str):
                        s = inner2
                except json.JSONDecodeError:
                    pass

    s = s.lstrip()
    if s.startswith('"') and not s.startswith('""'):
        s = s[1:]
    s = s.lstrip("\ufeff")

    return s


def partition_lines_by_service(
    lines: list[str],
) -> list[tuple[str, list[str], int, int]]:
    """Bucket by Spring ``--- [service] [``; stack tails follow last service.

    Each tuple is ``(key, bucket_lines, first_global_index, last_global_index)`` in
    first-seen bucket order (re-sort by ``first_global_index`` for chronological story).
    """
    order: list[str] = []
    buckets: dict[str, list[str]] = {}
    lo: dict[str, int] = {}
    hi: dict[str, int] = {}
    last_key = "_global"
    for gi, line in enumerate(lines):
        m = _SPRING_SERVICE_BUCKET.search(line)
        if m:
            last_key = m.group(1).strip()
            key = last_key
        elif _STACK_CONTINUATION.search(line):
            key = last_key
        else:
            key = "_global"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
            lo[key] = gi
            hi[key] = gi
        else:
            hi[key] = gi
        buckets[key].append(line)
    if len(order) == 1:
        k = order[0]
        return [(k, buckets[k], lo[k], hi[k])]
    return [(k, buckets[k], lo[k], hi[k]) for k in order]


def _first_global_anchor_index(lines: list[str], *, min_score: int) -> int | None:
    for gi, line in enumerate(lines):
        if _anchor_score(line) >= min_score:
            return gi
    return None


def _root_cause_signature(block: str) -> str | None:
    """Normalize first ``Caused by:`` payload for cross-bucket dedupe."""
    for ln in block.splitlines():
        m = re.search(r"(?i)Caused\s+by:\s*(.+)$", ln.strip())
        if not m:
            continue
        body = re.sub(r"\s+", " ", m.group(1).strip())[:240]
        if len(body) < 8:
            continue
        return _line_template(body)
    return None


def _bucket_failure_signature(block: str) -> str | None:
    """Signature for ``same failure again`` (Caused by, else first strong signal line)."""
    sig = _root_cause_signature(block)
    if sig:
        return sig
    for ln in block.splitlines():
        if not _KEY_SIGNAL.search(ln):
            continue
        cleaned = _TIMESTAMP_STRIP.sub("", ln).rstrip()
        if len(cleaned) < 12:
            continue
        m_sig = re.search(r"(?i)connection\s+refused", cleaned)
        if m_sig:
            cleaned = cleaned[: m_sig.end()]
        # Spring may use several ``[...]`` columns before the logger name; collapse for matching.
        cleaned = re.sub(r"(---)\s+(?:\[[^\]]*\]\s*)+", r"\1 [<hdr>] ", cleaned)
        cleaned = re.sub(r'"(?:https?://[^"]+)"', '"<URL>"', cleaned)
        cleaned = re.sub(
            r"/[a-z][a-z0-9._-]*/(?:default|actuator)\b",
            "/<app>/<cfg>",
            cleaned,
            flags=re.I,
        )
        return _line_template(cleaned[:400])
    return None


def _line_template(line: str) -> str:
    s = _TIMESTAMP_STRIP.sub("", line).rstrip()
    s = _URL_FOR_TEMPLATE.sub("<URL>", s)
    s = _HEX_FOR_TEMPLATE.sub("<HEX>", s)
    s = _NUM_FOR_TEMPLATE.sub("<N>", s)
    return s


def collapse_template_runs(lines: list[str], *, min_repeats: int) -> list[str]:
    """Stage 3: collapse consecutive lines that match after variable masking (template)."""
    if min_repeats < 2:
        min_repeats = 2
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        tpl = _line_template(lines[i])
        j = i + 1
        while j < n and _line_template(lines[j]) == tpl:
            j += 1
        run_len = j - i
        if run_len >= min_repeats:
            out.append(lines[i])
            out.append(f"... [Repeated {run_len - 1} times, same template] ...")
        else:
            out.extend(lines[i:j])
        i = j
    return out


def _anchor_score(line: str) -> int:
    """Stage 4: higher = more important to keep (smoking-gun finder)."""
    cleaned = _TIMESTAMP_STRIP.sub("", line).rstrip()
    s = cleaned
    score = 0
    if _CI_FAILURE_MARKERS.search(s):
        score += 6
    if re.search(r"(?i)\bcaused\s+by\s*:\s*\S", s):
        score += 5
    # Uppercase only — (?i) would match "error" inside "I/O error on GET".
    if re.search(r"(?:^|\s)(ERROR|FATAL|SEVERE)(?:\s|$)", s):
        score += 4
    if re.search(r"(?i)\b(assertionfailederror|assertionerror)\b", s):
        score += 5
    if _EXCEPTION_CLASS.search(s):
        if not re.match(r"^\s*INFO\s+", s):
            score += 3
        else:
            score += 1
    if _KEY_SIGNAL.search(s):
        score += 2
    if re.search(r"(?i)\bWARN\b", s) and _KEY_SIGNAL.search(s):
        score += 2
    if re.search(
        r"(?i)\b\w+ApplicationTests?\b|\b\w+Tests?\s+:\s+(Starting|Failed)",
        s,
    ):
        score += 2
    if re.search(r"(?i)Started\s+[\w.-]+\s+in\s+[\d.]+\s+seconds", s):
        score -= 3
    if re.match(r"^\s*INFO\s+", s) and score <= 0:
        score -= 1
    return score


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged: list[list[int]] = [[intervals[0][0], intervals[0][1]]]
    for s, e in intervals[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(a[0], a[1]) for a in merged]


def extract_anchor_windows(
    lines: list[str],
    *,
    before: int,
    after: int,
    context_before: int,
    max_chars: int,
    chronological: bool = False,
    max_anchor_points: int = 14,
) -> str:
    """Stage 4: keep merged windows around high-scoring lines; cap by ``max_chars``."""
    n = len(lines)
    if n == 0:
        return ""
    scores = [_anchor_score(lines[i]) for i in range(n)]
    anchors: list[int] = []
    if chronological:
        for th in (3, 2, 1):
            candidates = sorted(i for i in range(n) if scores[i] >= th)
            for i in candidates:
                if len(anchors) >= max_anchor_points:
                    break
                if i not in anchors:
                    anchors.append(i)
            if len(anchors) >= 6:
                break
    else:
        ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)
        anchors = [i for i in ranked if scores[i] >= 3][:36]
        if len(anchors) < 6:
            anchors = [i for i in ranked if scores[i] >= 2][:36]
        if len(anchors) < 4:
            anchors = [i for i in ranked if scores[i] >= 1][:40]
    if not anchors:
        tail = lines[max(0, n - 150) :]
        text = "\n".join(tail)
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + "\n... [truncated]"
        return text

    before_eff = max(before, context_before)
    intervals: list[tuple[int, int]] = []
    for i in anchors:
        lo = max(0, i - before_eff)
        hi = min(n, i + after + 1)
        intervals.append((lo, hi))
    merged = _merge_intervals(intervals)
    kept: set[int] = set()
    for lo, hi in merged:
        for j in range(lo, hi):
            kept.add(j)
    ordered = [lines[i] for i in sorted(kept)]
    text = "\n".join(ordered)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0] + "\n... [truncated]"


def _strip_noise_lines(lines: list[str], protected: set[int] | None = None) -> list[str]:
    out: list[str] = []
    prev_blank = False
    for idx, line in enumerate(lines):
        cleaned = _TIMESTAMP_STRIP.sub("", line).rstrip()
        if protected and idx in protected and _anchor_score(line) >= 4:
            prev_blank = False
            out.append(cleaned)
            continue
        if _NOISE_LINE.match(cleaned):
            if not prev_blank and out:
                prev_blank = True
            continue
        prev_blank = False
        out.append(cleaned)
    return out


_HORIZONTAL_WS_RUN = re.compile(r"[^\S\n]+")


def compress_filtered_whitespace(text: str) -> str:
    """Trim each line and collapse tabs/spaces to a single space (counts toward char caps)."""
    if not text:
        return ""
    out_lines: list[str] = []
    prev_blank = False
    for raw in text.splitlines():
        s = raw.rstrip()
        if not s:
            if not prev_blank:
                out_lines.append("")
            prev_blank = True
            continue
        prev_blank = False
        s = _HORIZONTAL_WS_RUN.sub(" ", s).strip()
        if s:
            out_lines.append(s)
    return "\n".join(out_lines).strip()


def _filter_logs_pipeline(raw_text: str) -> str:
    text = normalize_log_text(raw_text)
    lines = text.splitlines()
    if not lines:
        return ""

    g_first = _first_global_anchor_index(
        lines, min_score=settings.log_first_failure_min_score
    )

    if settings.log_partition_by_service:
        buckets = partition_lines_by_service(lines)
        buckets = sorted(buckets, key=lambda t: (t[2], t[0]))
    else:
        buckets = [("_all", lines, 0, max(0, len(lines) - 1))]

    n_buckets = len(buckets)
    per_cap = max(2000, settings.log_max_filtered_chars // max(1, n_buckets))

    parts: list[str] = []
    last_sig: str | None = None
    for key, bucket_lines, lo_i, hi_i in buckets:
        contains_first = (
            g_first is not None and lo_i <= g_first <= hi_i
        )
        after_lines = settings.log_anchor_after_lines
        before_lines = settings.log_anchor_before_lines
        cap_this = per_cap
        if settings.log_shrink_later_buckets and g_first is not None:
            if not contains_first:
                after_lines = min(
                    settings.log_anchor_after_lines,
                    settings.log_later_bucket_after_lines,
                )
                before_lines = min(
                    settings.log_anchor_before_lines,
                    settings.log_later_bucket_before_lines,
                )
                cap_this = min(per_cap, settings.log_later_bucket_max_chars)

        collapsed = collapse_template_runs(
            bucket_lines,
            min_repeats=settings.log_dedupe_min_consecutive,
        )
        excerpt = extract_anchor_windows(
            collapsed,
            before=before_lines,
            after=after_lines,
            context_before=settings.log_anchor_context_before,
            max_chars=cap_this,
            chronological=settings.log_anchor_chronological,
            max_anchor_points=settings.log_anchor_max_points,
        )
        if not excerpt.strip():
            continue
        elines = excerpt.splitlines()
        protected = {
            i
            for i, ln in enumerate(elines)
            if _anchor_score(ln) >= 4
        }
        cleaned_lines = _strip_noise_lines(elines, protected=protected)
        block = "\n".join(cleaned_lines).strip()
        if not block:
            continue

        sig = _bucket_failure_signature(block)
        if (
            settings.log_dedupe_same_root_bucket
            and sig
            and last_sig
            and sig == last_sig
            and key not in ("_all", "_global")
        ):
            block = (
                f"(Same root cause as the previous section; "
                f"[{key}] excerpt omitted for brevity.)"
            )
        elif sig:
            last_sig = sig

        if key != "_all" and n_buckets > 1:
            parts.append(f"--- [{key}] ---")
        parts.append(block)

    result = "\n\n".join(parts).strip()
    if settings.log_compress_whitespace:
        result = compress_filtered_whitespace(result)
    cap = settings.log_max_filtered_chars
    if len(result) > cap:
        result = result[:cap].rsplit("\n", 1)[0] + "\n... [truncated]"
    return result


def _filter_logs_legacy(raw_text: str) -> str:
    """Original line-by-line timestamp strip + noise regex only."""
    out: list[str] = []
    prev_blank = False

    for line in raw_text.splitlines():
        cleaned = _TIMESTAMP_STRIP.sub("", line).rstrip()
        if _NOISE_LINE.match(cleaned):
            if not prev_blank and out:
                prev_blank = True
            continue
        prev_blank = False
        out.append(cleaned)

    result = "\n".join(out).strip()
    if settings.log_compress_whitespace:
        result = compress_filtered_whitespace(result)
    return result


_embedding_model: SentenceTransformer | None = None
_es_client: Elasticsearch | None = None


def reset_es_client() -> None:
    """Drop cached ES client (e.g. after config URL change or in tests)."""
    global _es_client
    _es_client = None


def filter_logs(raw_text: str) -> str:
    """Filter CI logs for LLM / fingerprinting.

    Default: structural normalize → service buckets → template dedupe →
    anchor windows → noise strip (see ``LOG_FILTER_LEGACY``).
    """
    if settings.log_filter_legacy:
        return _filter_logs_legacy(raw_text)
    return _filter_logs_pipeline(raw_text)


# --- Jenkins / CI branch & exit patterns (metadata header) ---
_RE_FAILED_IN_BRANCH = re.compile(
    r"(?i)failed\s+in\s+branch\s*[:#\s]*([\w./\-]+)"
)
_RE_BRACKET_FAILED = re.compile(r"(?i)\[([^\]]+)\]\s*(?:FAILED|FAILURE)\b")
_RE_EXIT_CODE = re.compile(
    r"(?i)(?:exit\s+code|returned\s+exit\s+code|process\s+exited\s+with)"
    r"\s*[:=]?\s*(\d+)"
)
_EXIT_CODE_HINTS: dict[int, str] = {
    1: "general error",
    2: "misuse of shell builtin",
    126: "command not executable",
    127: "command not found",
    128: "invalid exit argument",
    130: "terminated (Ctrl+C)",
    137: "OOM / SIGKILL",
    139: "segfault / SIGSEGV",
    143: "terminated (SIGTERM)",
}


def _clean_primary_error_text(s: str, *, max_len: int = 220) -> str:
    """Human-readable error snippet for RAG (no timestamps, hex, file:line noise)."""
    t = _TIMESTAMP_STRIP.sub("", s).strip()
    t = re.sub(r"0x[0-9a-fA-F]+\b", "<hex>", t)
    t = re.sub(r"\b[0-9a-fA-F]{8,16}\b", "<id>", t)
    t = re.sub(r"\([^)]*\.(?:java|kt|scala|py|js|ts):\d+\)", "(<src>)", t)
    t = re.sub(r"~\[[^\]]*\]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        t = t[: max_len - 3].rsplit(" ", 1)[0] + "..."
    return t or "Unknown"


def _extract_primary_error(filtered_body: str) -> str:
    lines = [ln for ln in filtered_body.splitlines() if ln.strip()]
    if not lines:
        return "Unknown"
    best_i = 0
    best_s = -1
    for i, ln in enumerate(lines):
        sc = _anchor_score(ln)
        if sc > best_s:
            best_s = sc
            best_i = i
    line = lines[best_i]
    m_cb = re.search(
        r"(?i)Caused\s+by:\s*((?:[\w$]+\.)*[\w$]+(?:Exception|Error|Failure|Fault)\b)",
        line,
    )
    if m_cb:
        short = m_cb.group(1).strip().rsplit(".", 1)[-1]
        return _clean_primary_error_text(short)
    for m in _EXCEPTION_CLASS.finditer(line):
        short = m.group(1).rsplit(".", 1)[-1]
        return _clean_primary_error_text(short)
    if _KEY_SIGNAL.search(line):
        return _clean_primary_error_text(_KEY_SIGNAL.search(line).group(0))
    return _clean_primary_error_text(line)


def _extract_failing_branch_or_service(raw_text: str) -> str:
    text = normalize_log_text(raw_text)
    m = _RE_FAILED_IN_BRANCH.search(text)
    if m:
        return m.group(1).strip()
    m = _RE_BRACKET_FAILED.search(text)
    if m:
        return m.group(1).strip()
    for ln in text.splitlines():
        sm = _SPRING_SERVICE_BUCKET.search(ln)
        if sm and _anchor_score(ln) >= 2:
            return sm.group(1).strip()
    for ln in text.splitlines():
        sm = _SPRING_SERVICE_BUCKET.search(ln)
        if sm:
            return sm.group(1).strip()
    return "Unknown"


def _extract_exit_code(raw_text: str) -> tuple[str, str]:
    text = normalize_log_text(raw_text)
    m = _RE_EXIT_CODE.search(text)
    if not m:
        m = re.search(r"(?i)\bexit\s+code\s*[:=]?\s*(\d+)", text)
    if not m:
        return ("N/A", "none")
    code_s = m.group(1)
    try:
        code = int(code_s)
    except ValueError:
        return (code_s, "")
    hint = _EXIT_CODE_HINTS.get(code, "")
    return (str(code), hint)


def _trim_log_body(body: str, max_chars: int) -> str:
    if not body:
        return ""
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars].rsplit("\n", 1)[0]
    return cut + "\n... [truncated]"


class LogProcessor:
    """Builds filtered log text with a metadata summary header (outside body char cap)."""

    def format_with_metadata(self, raw_text: str, filtered_body: str) -> str:
        """Prepend metadata + delimiters. ``filtered_body`` is body-only (no header)."""
        primary = _extract_primary_error(filtered_body)
        service = _extract_failing_branch_or_service(raw_text)
        code, hint = _extract_exit_code(raw_text)
        if code == "N/A":
            code_display = "None"
        elif hint:
            code_display = f"{code} ({hint})"
        else:
            code_display = code

        raw_len = len(raw_text)
        body_trimmed = _trim_log_body(
            filtered_body, settings.log_body_max_chars
        )
        proc_len = len(body_trimmed)
        if raw_len <= 0:
            ratio_pct = 0.0
        else:
            ratio_pct = round(100.0 * (1.0 - proc_len / raw_len), 1)

        meta_lines = [
            "---",
            "[METADATA SUMMARY]",
            f"- Primary Error: {primary}",
            f"- Failing Service/Branch: {service}",
            f"- Critical Exit Code: {code_display}",
            (
                f"- Compression Ratio: original {raw_len} chars → "
                f"compressed {proc_len} chars (~{ratio_pct}% noise removed vs raw)"
            ),
            "---",
        ]
        return "\n".join(meta_lines) + "\n" + body_trimmed + "\n---"

    def process(self, raw_text: str) -> str:
        """Filter logs, trim body to ``LOG_BODY_MAX_CHARS``, prepend metadata."""
        body = filter_logs(raw_text)
        return self.format_with_metadata(raw_text, body)


def generate_fingerprint(filtered_logs: str, stage_name: str) -> str:
    """Build a compact, searchable fingerprint from error signals + stage name."""
    exception_types: list[str] = []
    caused_by_msgs: list[str] = []
    error_messages: list[str] = []
    key_signals: list[str] = []
    seen_exceptions: set[str] = set()

    for line in filtered_logs.splitlines():
        if re.match(r"^\s+at\s+\S", line) or re.match(r"^\s*Caused by:\s*$", line):
            continue
        for m in _EXCEPTION_CLASS.finditer(line):
            short = m.group(1).rsplit(".", 1)[-1]
            if short not in seen_exceptions and len(exception_types) < 6:
                seen_exceptions.add(short)
                exception_types.append(short)

        m = _CAUSED_BY.search(line)
        if m:
            caused_by_msgs.append(m.group(1).strip()[:200])

        m = _ERROR_LINE.search(line)
        if m:
            msg = m.group(1).strip()
            if msg and len(msg) > 5:
                error_messages.append(msg[:200])

        for sig_match in _KEY_SIGNAL.finditer(line):
            sig = sig_match.group(0).strip().lower()
            if sig not in key_signals:
                key_signals.append(sig)

    parts: list[str] = [f"stage:{stage_name}"]
    if exception_types:
        parts.append("exceptions: " + ", ".join(exception_types[:6]))
    if caused_by_msgs:
        parts.append("root_cause: " + caused_by_msgs[-1])
    elif error_messages:
        parts.append("error: " + error_messages[0])
    if key_signals:
        parts.append("signals: " + ", ".join(key_signals[:8]))

    return " | ".join(parts)


def _get_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        logger.info("Loading embedding model: %s", settings.embedding_model)
        _embedding_model = SentenceTransformer(settings.embedding_model)
    return _embedding_model


def embed_text(text: str) -> list[float]:
    """Embed text using all-MiniLM and return a 384-dim vector."""
    model = _get_embedding_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def _get_es_client() -> Elasticsearch:
    global _es_client
    if _es_client is None:
        logger.info("Elasticsearch client hosts=%s", settings.elasticsearch_url)
        kwargs: dict[str, Any] = {
            "hosts": [settings.elasticsearch_url],
            "request_timeout": 30,
        }
        if settings.elasticsearch_api_key:
            kwargs["api_key"] = settings.elasticsearch_api_key
        _es_client = Elasticsearch(**kwargs)
    return _es_client


def _solution_properties() -> dict[str, Any]:
    return {
        "fingerprint_text": {"type": "text"},
        "fingerprint_vector": {
            "type": "dense_vector",
            "dims": 384,
            "index": True,
            "similarity": "cosine",
        },
        "solution": {"type": "text"},
        "solution_score": {"type": "float"},
        "job_name": {"type": "keyword"},
        "stage_name": {"type": "keyword"},
        "build_number": {"type": "integer"},
        "created_at": {"type": "date"},
    }


def ensure_index() -> None:
    """Create the ES index with dense-vector mapping if it doesn't exist."""
    es = _get_es_client()
    idx = settings.elasticsearch_index
    if es.indices.exists(index=idx):
        return
    es.indices.create(
        index=idx,
        mappings={"properties": _solution_properties()},
    )
    logger.info("Created ES index: %s", idx)


def ensure_context_index() -> None:
    """Optional index for filtered-log snippets keyed by fingerprint (extra LLM context)."""
    es = _get_es_client()
    idx = settings.elasticsearch_context_index
    if es.indices.exists(index=idx):
        return
    es.indices.create(
        index=idx,
        mappings={
            "properties": {
                "fingerprint_text": {"type": "text"},
                "fingerprint_vector": {
                    "type": "dense_vector",
                    "dims": 384,
                    "index": True,
                    "similarity": "cosine",
                },
                "filtered_excerpt": {"type": "text"},
                "created_at": {"type": "date"},
            }
        },
    )
    logger.info("Created ES context index: %s", idx)


def _combined_rank(hit: dict[str, Any]) -> float:
    src = hit.get("_source") or {}
    base = float(hit.get("_score") or 0.0)
    q = float(src.get("solution_score", 1.0))
    return base * q


def search_similar_solutions(fingerprint: str) -> list[dict[str, Any]]:
    """Embed the fingerprint, knn-search ES, return up to 3 best-ranked matches."""
    es = _get_es_client()
    vector = embed_text(fingerprint)

    try:
        resp = es.search(
            index=settings.elasticsearch_index,
            knn={
                "field": "fingerprint_vector",
                "query_vector": vector,
                "k": 12,
                "num_candidates": 50,
            },
            source=[
                "fingerprint_text",
                "solution",
                "solution_score",
                "job_name",
                "stage_name",
                "build_number",
            ],
        )
    except Exception:
        logger.exception("ES knn search failed")
        return []

    hits = resp["hits"]["hits"]
    ranked = [h for h in hits if float(h.get("_score") or 0.0) >= settings.similarity_threshold]
    if not ranked:
        ranked = hits[:3]
    ranked.sort(key=_combined_rank, reverse=True)
    results: list[dict[str, Any]] = []
    for hit in ranked[: settings.similarity_top_k]:
        results.append({"score": float(hit.get("_score") or 0.0), **hit["_source"]})
    return results


def store_solution(
    fingerprint: str,
    solution: str,
    job_name: str = "",
    stage_name: str = "",
    build_number: int = 0,
    solution_score: float = 1.0,
) -> str:
    """Embed and index a verified solution (multiple docs per fingerprint allowed)."""
    es = _get_es_client()
    vector = embed_text(fingerprint)
    doc_id = str(uuid.uuid4())

    es.index(
        index=settings.elasticsearch_index,
        id=doc_id,
        document={
            "fingerprint_text": fingerprint,
            "fingerprint_vector": vector,
            "solution": solution,
            "solution_score": solution_score,
            "job_name": job_name,
            "stage_name": stage_name,
            "build_number": build_number,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    logger.info("Stored solution doc_id=%s for %s #%d", doc_id, job_name, build_number)
    return doc_id


def store_filtered_context(fingerprint: str, filtered_excerpt: str) -> str:
    """Store a filtered-log excerpt for retrieval keyed by fingerprint embedding."""
    es = _get_es_client()
    ensure_context_index()
    vector = embed_text(fingerprint)
    doc_id = str(uuid.uuid4())
    es.index(
        index=settings.elasticsearch_context_index,
        id=doc_id,
        document={
            "fingerprint_text": fingerprint,
            "fingerprint_vector": vector,
            "filtered_excerpt": filtered_excerpt,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    logger.info("Stored context snippet doc_id=%s", doc_id)
    return doc_id


def prune_stale_documents() -> None:
    """Drop documents older than retention windows (per-index ``created_at``)."""
    es = _get_es_client()
    for idx, days in (
        (settings.elasticsearch_index, settings.elasticsearch_retention_solutions_days),
        (settings.elasticsearch_context_index, settings.elasticsearch_retention_context_days),
    ):
        if days <= 0:
            continue
        try:
            if not es.indices.exists(index=idx):
                continue
        except Exception:
            logger.exception("ES exists check failed for %s", idx)
            continue
        cutoff = datetime.now(UTC) - timedelta(days=days)
        cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            es.delete_by_query(
                index=idx,
                query={"range": {"created_at": {"lt": cutoff_s}}},
                refresh=True,
                conflicts="proceed",
            )
            logger.info("Pruned documents in %s older than %s", idx, cutoff_s)
        except Exception:
            logger.exception("Prune failed for index %s", idx)
