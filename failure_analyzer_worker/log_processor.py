"""Log filtering, fingerprint generation, and Elasticsearch operations.

This module owns three concerns that feed the LangGraph analysis pipeline:
  1. filter_logs        – strip timestamps, CI boilerplate, package downloads
  2. generate_fingerprint – extract error signals into a compact search key
  3. ES helpers         – embed fingerprints with all-MiniLM, knn-search, store
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Any

from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer

from .config import settings

logger = logging.getLogger(__name__)

# ===================================================================
# Timestamp patterns – stripped from the *beginning* of every line
# ===================================================================
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

# ===================================================================
# ANSI escape codes – stripped inline (not whole-line discard)
# ===================================================================
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# ===================================================================
# Noise lines – entire line is discarded if matched
# ===================================================================
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

    # Python pip / virtualenv noise
    r"^\s*(?:Requirement already satisfied|Collecting\s|Using cached\s|Installing collected|Successfully installed|"
    r"Creating virtualenv|Activating virtualenv|pip install)\b|"

    # Go build / test noise
    r"^\s*(?:go: downloading\s|go: extracting\s|ok\s+\S+\s+\d+\.\d+s|"
    r"\?\s+\S+\s+\[no test files\])|"

    # Terraform noise
    r"^\s*(?:Terraform has been|Initializing provider|Initializing modules|"
    r"Terraform will perform|Plan:\s+\d+ to add)|"

    # Rust / Cargo noise
    r"^\s*(?:Compiling\s+\S+\s+v|Downloading\s+crates|Updating\s+crates\.io)|"

    # Generic CI markers with no diagnostic value
    r"^\s*(?:Started by|Running in|Established SSH|"
    r"\+\s*(?:echo|export|cd|mkdir|chmod|set)\s)"
)

# ===================================================================
# Fingerprint extraction patterns
# ===================================================================
_EXCEPTION_CLASS = re.compile(
    r"\b([a-zA-Z_$][\w$]*(?:\.[a-zA-Z_$][\w$]*)*"
    r"(?:Exception|Error|Failure|Fault))\b"
    r"(?![\w$]*(?:Handler|Formatter|Factory|Builder|Callback|Listener|Logger|"
    r"Writer|Reader|Config|Code|Message|Page|Response|Boundary))"
)
_CAUSED_BY = re.compile(
    r"Caused\s+by:\s*(\S+(?:Exception|Error|Failure|Fault)\b[^\n]*)",
    re.IGNORECASE,
)
_ERROR_LINE = re.compile(
    r"(?:^|\s)(?:ERROR|FATAL|SEVERE)\s+(.+)", re.IGNORECASE
)
_PYTHON_TRACEBACK = re.compile(r"Traceback \(most recent call last\):")
_PYTHON_FINAL_ERROR = re.compile(
    r"^([A-Z]\w*(?:Error|Exception|Warning))\s*:?\s*(.*)", re.MULTILINE
)
_GO_PANIC = re.compile(r"^panic:\s*(.+)", re.MULTILINE)
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
    r"assert(?:ion)?(?:\s+(?:failed|error))?|"
    r"command\s+not\s+found|syntax\s+error\s+near|"
    r"no\s+such\s+file\s+or\s+directory|"
    r"image\s+pull\s+back\s*off|crash\s*loop\s*back\s*off|"
    r"exec\s+format\s+error"
    r")\b"
)

# ===================================================================
# Lazy singletons
# ===================================================================
_embedding_model: SentenceTransformer | None = None
_es_client: Elasticsearch | None = None


# ===================================================================
# Public: log filtering
# ===================================================================

def filter_logs(raw_text: str) -> str:
    """Strip timestamps, ANSI codes, CI boilerplate, and collapse duplicates."""
    out: list[str] = []
    prev_blank = False
    prev_line: str | None = None
    dup_count = 0

    for line in raw_text.splitlines():
        cleaned = _TIMESTAMP_STRIP.sub("", line).rstrip()
        cleaned = _ANSI_ESCAPE.sub("", cleaned)
        if _NOISE_LINE.match(cleaned):
            if not prev_blank and out:
                prev_blank = True
            continue
        prev_blank = False

        if cleaned == prev_line:
            dup_count += 1
            if dup_count < 2:
                out.append(cleaned)
            continue

        if dup_count >= 2:
            extra = dup_count - 1
            out.append(f"... ({extra} more identical line{'s' if extra != 1 else ''})")

        prev_line = cleaned
        dup_count = 0
        out.append(cleaned)

    if dup_count >= 2:
        extra = dup_count - 1
        out.append(f"... ({extra} more identical line{'s' if extra != 1 else ''})")

    return "\n".join(out).strip()


# ===================================================================
# Public: fingerprint generation
# ===================================================================

def generate_fingerprint(
    filtered_logs: str, stage_name: str, error_class: str = ""
) -> str:
    """Build a compact, searchable fingerprint from error signals + stage name."""
    exception_types: list[str] = []
    caused_by_msgs: list[str] = []
    error_messages: list[str] = []
    key_signals: list[str] = []
    seen_exceptions: set[str] = set()

    for line in filtered_logs.splitlines():
        for m in _EXCEPTION_CLASS.finditer(line):
            short = m.group(1).rsplit(".", 1)[-1]
            if short not in seen_exceptions:
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

    if _PYTHON_TRACEBACK.search(filtered_logs):
        for m in _PYTHON_FINAL_ERROR.finditer(filtered_logs):
            short = m.group(1)
            if short not in seen_exceptions:
                seen_exceptions.add(short)
                exception_types.append(short)
            if not caused_by_msgs:
                msg = m.group(2).strip()
                if msg:
                    caused_by_msgs.append(msg[:200])

    if not exception_types:
        for m in _GO_PANIC.finditer(filtered_logs):
            exception_types.append("panic")
            if not caused_by_msgs:
                caused_by_msgs.append(m.group(1).strip()[:200])
            break

    parts: list[str] = [f"stage:{stage_name}"]
    if error_class and error_class != "unknown":
        parts.append(f"class:{error_class}")
    if exception_types:
        parts.append("exceptions: " + ", ".join(exception_types[:10]))
    if caused_by_msgs:
        parts.append("root_cause: " + caused_by_msgs[-1])
    elif error_messages:
        parts.append("error: " + error_messages[0])
    if key_signals:
        parts.append("signals: " + ", ".join(key_signals[:8]))

    return " | ".join(parts)


# ===================================================================
# Embedding helpers
# ===================================================================

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


# ===================================================================
# Elasticsearch helpers
# ===================================================================

def _get_es_client() -> Elasticsearch:
    global _es_client
    if _es_client is None:
        kwargs: dict[str, Any] = {
            "hosts": [settings.elasticsearch_url],
            "request_timeout": 30,
        }
        if settings.elasticsearch_api_key:
            kwargs["api_key"] = settings.elasticsearch_api_key
        _es_client = Elasticsearch(**kwargs)
    return _es_client


def ensure_index() -> None:
    """Create the ES index with dense-vector mapping if it doesn't exist."""
    es = _get_es_client()
    idx = settings.elasticsearch_index
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
                "solution": {"type": "text"},
                "job_name": {"type": "keyword"},
                "stage_name": {"type": "keyword"},
                "build_number": {"type": "integer"},
                "created_at": {"type": "date"},
            }
        },
    )
    logger.info("Created ES index: %s", idx)


def search_similar_solutions(fingerprint: str) -> list[dict[str, Any]]:
    """Embed the fingerprint and knn-search ES for past solutions above threshold."""
    es = _get_es_client()
    vector = embed_text(fingerprint)

    try:
        resp = es.search(
            index=settings.elasticsearch_index,
            knn={
                "field": "fingerprint_vector",
                "query_vector": vector,
                "k": settings.similarity_top_k,
                "num_candidates": 50,
            },
            source=["fingerprint_text", "solution", "job_name",
                     "stage_name", "build_number"],
        )
    except Exception:
        logger.exception("ES knn search failed")
        return []

    results: list[dict[str, Any]] = []
    for hit in resp["hits"]["hits"]:
        score = hit.get("_score", 0.0)
        if score >= settings.similarity_threshold:
            results.append({"score": score, **hit["_source"]})
    return results


def store_solution(
    fingerprint: str,
    solution: str,
    job_name: str = "",
    stage_name: str = "",
    build_number: int = 0,
) -> str:
    """Embed and index a verified solution so future builds can find it."""
    es = _get_es_client()
    vector = embed_text(fingerprint)
    doc_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]

    es.index(
        index=settings.elasticsearch_index,
        id=doc_id,
        document={
            "fingerprint_text": fingerprint,
            "fingerprint_vector": vector,
            "solution": solution,
            "job_name": job_name,
            "stage_name": stage_name,
            "build_number": build_number,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    logger.info("Stored solution doc_id=%s for %s #%d", doc_id, job_name, build_number)
    return doc_id
