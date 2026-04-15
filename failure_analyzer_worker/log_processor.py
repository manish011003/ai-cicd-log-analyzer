"""Log filtering, fingerprinting, and Elasticsearch (embeddings + knn + store)."""

from __future__ import annotations

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
    # Java stack frames / trace filler
    r"^\s+at\s+(?:[\w.$]+\.)+[\w$]+\([^)]*\)\s*(?:~\[.*)?\s*$|"
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

_embedding_model: SentenceTransformer | None = None
_es_client: Elasticsearch | None = None


def reset_es_client() -> None:
    """Drop cached ES client (e.g. after config URL change or in tests)."""
    global _es_client
    _es_client = None


def filter_logs(raw_text: str) -> str:
    """Strip timestamps, CI boilerplate, and package-download noise."""
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

    return "\n".join(out).strip()


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
