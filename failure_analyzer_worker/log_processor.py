"""Minimal log cleaner + Elasticsearch retrieval.

Design notes:

* The ``jenkins_failure_listener`` already crops a tight window around the real
  error anchors before sending us ``log_excerpt``.  This module's job is to
  *clean* that excerpt (timestamps, blank runs, Jenkins/Maven/Spring/Docker
  boilerplate) and feed it to the LLM as-is.
* We deliberately do **not** re-score, re-bucket, or re-anchor the text.  The
  previous pipeline did all of that and was the main cause of "wrong analysis"
  symptoms (it kept relocating the centre of the excerpt to ``BUILD FAILURE``
  marker lines and shuffling Spring services into separate buckets).
* Retrieval (``search_similar_solutions``) now strictly honours
  ``SIMILARITY_THRESHOLD`` -- below the threshold we return ``[]`` so the
  LangGraph router falls back to ``analyze_fresh`` instead of grounding the
  LLM in an irrelevant "verified past solution".
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .config import settings

if TYPE_CHECKING:  # heavy deps loaded lazily inside _get_*; keeps `import log_processor` cheap
    from elasticsearch import Elasticsearch
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


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

    No scoring, no bucketing, no anchor windows -- the listener already narrowed
    the log to the relevant region.
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


# ── Tiny metadata for the UI (NOT used for routing or scoring) ────────────────

_EXCEPTION_LINE = re.compile(r"\b((?:[\w$]+\.)*[\w$]+(?:Exception|Error|Failure|Fault))\b")
_CAUSED_BY = re.compile(r"(?i)Caused\s+by:\s*(.+)")
_ERROR_MARKER = re.compile(r"(?:^|\s)(?:ERROR|FATAL|SEVERE)\b\s*[:\-]?\s*(.+)")
_EXIT_CODE = re.compile(
    r"(?i)(?:exit\s+code|returned\s+exit\s+code|process\s+exited\s+with)"
    r"\s*[:=]?\s*(\d+)"
)


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _primary_error(filtered: str) -> str:
    """Caused-by → Exception class → ERROR/FATAL/SEVERE line → 'Unknown'."""
    return (
        _first(_CAUSED_BY, filtered)
        or _first(_EXCEPTION_LINE, filtered)
        or _first(_ERROR_MARKER, filtered)
        or "Unknown"
    )


# ── Fingerprint for ES retrieval (focused, low-noise) ─────────────────────────


def generate_fingerprint(filtered: str, stage_name: str) -> str:
    """Caused-by → first 4 exception types → first ERROR line → stage.

    Deliberately omits generic phrases (``not found`` / ``timed out`` /
    ``refused``) that previously polluted embeddings and produced weak
    Elasticsearch neighbours.
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


# ── LogProcessor wrapper (kept for callers / preprocess node) ─────────────────


class LogProcessor:
    """Filtered body + a small metadata header.

    The header is for the UI only; the LLM should rely on the body, not the
    header (the header's heuristics are best-effort).
    """

    def format_with_metadata(self, raw: str, body: str) -> str:
        primary = _primary_error(body)
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


# ── Elasticsearch / embeddings ────────────────────────────────────────────────

_embedding_model: SentenceTransformer | None = None
_es_client: Elasticsearch | None = None


def reset_es_client() -> None:
    """Drop cached ES client (e.g. after config URL change or in tests)."""
    global _es_client
    _es_client = None


def _get_embedding_model() -> SentenceTransformer:
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer  # heavy: torch

        logger.info("Loading embedding model: %s", settings.embedding_model)
        _embedding_model = SentenceTransformer(settings.embedding_model)
    return _embedding_model


def embed_text(text: str) -> list[float]:
    """Embed text using the configured sentence-transformer (default 384-dim)."""
    return _get_embedding_model().encode(text, normalize_embeddings=True).tolist()


def _get_es_client() -> Elasticsearch:
    global _es_client
    if _es_client is None:
        from elasticsearch import Elasticsearch

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
    es.indices.create(index=idx, mappings={"properties": _solution_properties()})
    logger.info("Created ES index: %s", idx)


def ensure_context_index() -> None:
    """Optional index for filtered-log snippets keyed by fingerprint."""
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
    """Embed the fingerprint, knn-search ES, return matches that pass the threshold.

    Returns ``[]`` when nothing clears ``SIMILARITY_THRESHOLD`` so the LangGraph
    router falls back to ``analyze_fresh`` instead of grounding the LLM in a
    weak / irrelevant "verified past solution".
    """
    es = _get_es_client()
    vector = embed_text(fingerprint)
    try:
        resp = es.search(
            index=settings.elasticsearch_index,
            knn={
                "field": "fingerprint_vector",
                "query_vector": vector,
                "k": max(settings.similarity_top_k, 5),
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

    ranked = [
        h for h in resp["hits"]["hits"]
        if float(h.get("_score") or 0.0) >= settings.similarity_threshold
    ]
    ranked.sort(key=_combined_rank, reverse=True)
    return [
        {"score": float(h.get("_score") or 0.0), **h["_source"]}
        for h in ranked[: settings.similarity_top_k]
    ]


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
    doc_id = str(uuid.uuid4())
    es.index(
        index=settings.elasticsearch_index,
        id=doc_id,
        document={
            "fingerprint_text": fingerprint,
            "fingerprint_vector": embed_text(fingerprint),
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
    doc_id = str(uuid.uuid4())
    es.index(
        index=settings.elasticsearch_context_index,
        id=doc_id,
        document={
            "fingerprint_text": fingerprint,
            "fingerprint_vector": embed_text(fingerprint),
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
