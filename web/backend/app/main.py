from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import db
from app.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _json_safe(val: Any) -> Any:
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, dict):
        return {k: _json_safe(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_json_safe(v) for v in val]
    return val


async def _retention_loop() -> None:
    """Background janitor that purges old sessions + messages on a timer.

    Runs once on startup so long-lived deployments always converge to the
    configured TTL, even if the schedule never fires (e.g. crashy container).
    """
    interval_hours = max(settings.retention_run_interval_hours, 0)
    while True:
        try:
            result = db.run_retention(
                session_days=settings.session_retention_days,
                message_days=settings.message_retention_days,
            )
            logger.info(
                "Retention sweep  sessions_deleted=%d  messages_deleted=%d  "
                "(session_ttl=%dd message_ttl=%dd)",
                result["sessions_deleted"],
                result["messages_deleted"],
                settings.session_retention_days,
                settings.message_retention_days,
            )
        except Exception:
            logger.exception("Retention sweep failed")
        if interval_hours <= 0:
            return
        await asyncio.sleep(interval_hours * 3600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_schema()
    task: asyncio.Task[None] | None = None
    if settings.retention_run_interval_hours > 0:
        task = asyncio.create_task(_retention_loop(), name="retention-janitor")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title="CI Analyzer Web API", lifespan=lifespan)

# CORS_ORIGINS is a comma-separated list (or "*" for any origin).
_cors_origins = [o.strip() for o in (settings.cors_origins or "*").split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SessionCreate(BaseModel):
    job_full_name: str
    build_number: int
    stage_name: str = ""
    build_url: str = ""
    fingerprint: str = ""
    analysis: str = ""
    suggested_fix: str = ""
    filtered_logs: str = ""
    raw_logs: str = ""
    matched_solution: str = ""
    match_score: float = 0.0
    recommendation: str = ""
    similar_past: list[dict[str, Any]] = Field(default_factory=list)
    # Structural-filter telemetry from the worker (confidence, detected
    # stack, primary location, collapse stats). Kept as an opaque dict so
    # the web-backend doesn't need to track the worker's filter schema.
    filter_meta: dict[str, Any] = Field(default_factory=dict)


class MessageIn(BaseModel):
    content: str
    # Opt-in: include the raw (pre-filter) log excerpt in the LLM context.
    # More tokens / slower, but preserves detail the filter dropped.
    use_full_log: bool = False


class FeedbackIn(BaseModel):
    decision: str  # accept | reject


class AgentChatIn(BaseModel):
    message: str
    run_id: str = ""
    use_full_log: bool = False


_ERR_CLASS_PATTERNS = [
    re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Exception|Error|Failure))\b"),
    re.compile(r"\b(error|failed|fatal)\b", re.IGNORECASE),
]


def _extract_error_class(row: dict[str, Any]) -> str:
    for field in ("fingerprint", "analysis", "suggested_fix", "filtered_logs"):
        text = str(row.get(field) or "")
        if not text:
            continue
        for pat in _ERR_CLASS_PATTERNS:
            m = pat.search(text)
            if m:
                token = m.group(1) if m.lastindex else m.group(0)
                return token[:80]
    return "UnknownError"


def _summarize_filter_meta(meta: Any) -> dict[str, Any]:
    """Project the worker's filter telemetry into a UI-friendly shape.

    We deliberately keep this lossy: the dashboard only needs the badges
    (confidence, detected stack, primary location) and a couple of
    compression numbers — the full structural metadata stays in Postgres
    for future deep-dives but is not pushed over the wire on every list
    refresh.
    """
    if not isinstance(meta, dict) or not meta:
        return {}
    primary = meta.get("primary_location") or None
    if not isinstance(primary, dict):
        primary = None
    detectors = meta.get("activated_detectors") or []
    if not isinstance(detectors, list):
        detectors = []
    out: dict[str, Any] = {
        "confidence": str(meta.get("confidence") or "").upper() or None,
        "activated_detectors": [str(d) for d in detectors if d],
        "primary_location": primary,
        "raw_chars": int(meta.get("raw_chars") or 0),
        "body_chars": int(meta.get("body_chars") or 0),
        "body_tokens": int(meta.get("body_tokens") or 0),
        "selected_count": int(meta.get("selected_count") or 0),
        "baseline_version": meta.get("baseline_version") or None,
    }
    if isinstance(meta.get("collapse_stats"), dict):
        out["collapse_stats"] = {str(k): int(v) for k, v in meta["collapse_stats"].items()}
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _to_result_item(row: dict[str, Any]) -> dict[str, Any]:
    feedback_status = str(row.get("feedback_status") or "")
    filter_summary = _summarize_filter_meta(row.get("filter_meta"))
    return {
        "run_id": str(row.get("id")),
        "job_full_name": row.get("job_full_name") or "",
        "build_number": int(row.get("build_number") or 0),
        "stage_name": row.get("stage_name") or "",
        "stage_id": None,
        "error_class": _extract_error_class(row),
        "signature": (row.get("fingerprint") or "")[:500],
        "cleaned_log": row.get("filtered_logs") or "",
        "source": row.get("recommendation") or "session",
        "timestamp": row.get("created_at"),
        "analysis": row.get("analysis") or "",
        "suggested_fix": row.get("suggested_fix") or "",
        "feedback_status": feedback_status or "new",
        # Empty dict when the worker did not provide telemetry (older
        # builds, legacy rows) so the UI can branch on Object.keys.
        "filter_meta": filter_summary,
    }


def _build_chat_payload(
    row: dict[str, Any] | None,
    payload: list[dict[str, str]],
    *,
    use_full_log: bool = False,
) -> dict[str, Any]:
    if not row:
        return {
            "messages": payload,
            "fingerprint": "",
            "log_excerpt": "",
            "job_name": "",
            "stage_name": "",
            "build_number": 0,
            "analysis": "",
            "suggested_fix": "",
            "use_full_log": use_full_log,
        }
    # When the caller asks for full-log context, pass the raw Jenkins excerpt
    # we stored at ingest time; otherwise use the compact filtered excerpt so
    # the LLM call stays cheap by default.
    excerpt = (
        row.get("raw_logs")
        if use_full_log and row.get("raw_logs")
        else row.get("filtered_logs")
    ) or ""
    return {
        "messages": payload,
        "fingerprint": row.get("fingerprint") or "",
        "log_excerpt": excerpt,
        "job_name": row.get("job_full_name") or "",
        "stage_name": row.get("stage_name") or "",
        "build_number": int(row.get("build_number") or 0),
        "analysis": row.get("analysis") or "",
        "suggested_fix": row.get("suggested_fix") or "",
        "use_full_log": use_full_log,
    }


def _worker_chat(
    messages: list[dict[str, str]],
    row: dict[str, Any] | None = None,
    *,
    use_full_log: bool = False,
) -> str:
    url = f"{settings.worker_base_url.rstrip('/')}/chat/turn"
    try:
        r = httpx.post(
            url,
            headers={"X-Api-Key": settings.worker_api_key},
            json=_build_chat_payload(row, messages, use_full_log=use_full_log),
            timeout=300.0,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        logger.error("worker chat HTTP %s: %s", exc.response.status_code, body)
        status = 429 if exc.response.status_code == 429 else 502
        raise HTTPException(status_code=status, detail=body or str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.exception("worker chat failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    data = r.json()
    return str(data.get("content", ""))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _db_target(url: str) -> dict[str, Any]:
    try:
        p = urlparse(url)
        dbname = (p.path or "").lstrip("/").split("?", 1)[0] or ""
        return {
            "scheme": p.scheme,
            "host": p.hostname,
            "port": p.port,
            "dbname": dbname,
        }
    except Exception:
        return {"error": "could_not_parse_database_url"}


@app.get("/api/diagnostics")
def diagnostics() -> dict[str, Any]:
    """Redacted wiring snapshot for debugging multi-service setups."""
    listener_reachable = False
    listener_detail: str | None = None
    try:
        r = httpx.get(
            f"{settings.listener_base_url.rstrip('/')}/health",
            timeout=3.0,
        )
        listener_reachable = r.status_code == 200
        listener_detail = r.text[:200] if not listener_reachable else None
    except Exception as exc:
        listener_detail = str(exc)

    worker_reachable = False
    worker_detail: str | None = None
    try:
        r = httpx.get(f"{settings.worker_base_url.rstrip('/')}/health", timeout=3.0)
        worker_reachable = r.status_code == 200
        worker_detail = r.text[:200] if not worker_reachable else None
    except Exception as exc:
        worker_detail = str(exc)

    return {
        "database_target": _db_target(settings.database_url),
        "listener_base_url": settings.listener_base_url,
        "listener_http_reachable": listener_reachable,
        "listener_http_error": listener_detail if not listener_reachable else None,
        "worker_base_url": settings.worker_base_url,
        "worker_http_reachable": worker_reachable,
        "worker_http_error": worker_detail if not worker_reachable else None,
    }


@app.post("/api/maintenance/purge")
def run_maintenance_purge(
    session_days: int | None = Query(default=None, ge=1, le=3650),
    message_days: int | None = Query(default=None, ge=1, le=3650),
) -> dict[str, Any]:
    """Run retention immediately. Overrides default TTLs if query params given.

    Intended for admin / ops — e.g. ``curl -X POST /api/maintenance/purge?message_days=30``.
    """
    s_days = session_days if session_days is not None else settings.session_retention_days
    m_days = message_days if message_days is not None else settings.message_retention_days
    result = db.run_retention(session_days=s_days, message_days=m_days)
    return {
        "session_retention_days": s_days,
        "message_retention_days": m_days,
        **result,
    }


@app.get("/api/sessions")
def list_sessions() -> list[dict[str, Any]]:
    rows = db.list_sessions()
    return [_json_safe(dict(r)) for r in rows]


@app.post("/api/sessions")
def create_session(body: SessionCreate) -> dict[str, Any]:
    sid = db.insert_session(
        job_full_name=body.job_full_name,
        build_number=body.build_number,
        stage_name=body.stage_name,
        build_url=body.build_url,
        fingerprint=body.fingerprint,
        analysis=body.analysis,
        suggested_fix=body.suggested_fix,
        filtered_logs=body.filtered_logs,
        raw_logs=body.raw_logs,
        matched_solution=body.matched_solution,
        match_score=body.match_score,
        recommendation=body.recommendation,
        similar_past=body.similar_past,
        filter_meta=body.filter_meta,
    )
    row = db.fetch_session(sid)
    return {"id": sid, "session": _json_safe(dict(row)) if row else {}}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    row = db.fetch_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    msgs = db.list_messages(session_id)
    return {
        "session": _json_safe(dict(row)),
        "messages": [_json_safe(dict(m)) for m in msgs],
    }


@app.post("/api/sessions/{session_id}/messages")
def post_message(session_id: str, body: MessageIn) -> dict[str, Any]:
    row = db.fetch_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    db.insert_message(session_id, "user", body.content)
    msgs = db.list_messages(session_id)
    payload = [{"role": m["role"], "content": m["content"]} for m in msgs]
    text = _worker_chat(payload, dict(row), use_full_log=body.use_full_log)
    db.insert_message(session_id, "assistant", text)
    return {"role": "assistant", "content": text}


@app.post("/api/sessions/{session_id}/feedback")
def post_feedback(session_id: str, body: FeedbackIn) -> dict[str, Any]:
    row = db.fetch_session(session_id)
    if not row:
        raise HTTPException(status_code=404, detail="session not found")
    d = body.decision.lower().strip()
    if d not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="decision must be accept or reject")

    # Idempotency: a session that already has terminal feedback should NEVER
    # generate another /store-solution write. Without this guard the UI's
    # "Accept" button (which the user can click multiple times before the
    # dashboard refresh comes back) created duplicate kNN docs in ES — every
    # re-click was a fresh ``es.index(...)``. We short-circuit here and
    # report the existing status so the UI can update without side effects.
    current = (row.get("feedback_status") or "").strip().lower()
    if current in ("accepted", "rejected"):
        return {"status": current, "idempotent": True}

    if d == "accept":
        solution = (row["suggested_fix"] or row["analysis"] or "").strip()
        if not solution:
            raise HTTPException(status_code=400, detail="nothing to store as solution")
        url = f"{settings.worker_base_url.rstrip('/')}/store-solution"
        try:
            r = httpx.post(
                url,
                headers={"X-Api-Key": settings.worker_api_key},
                json={
                    "fingerprint": row["fingerprint"] or "",
                    "solution": solution,
                    "job_name": row["job_full_name"] or "",
                    "stage_name": row["stage_name"] or "",
                    "build_number": int(row["build_number"] or 0),
                    "solution_score": 1.0,
                    # Tell the worker which Postgres session this is so it can
                    # build a deterministic ES doc id (defence-in-depth: even
                    # if some other caller bypasses this idempotency guard,
                    # the worker won't write a duplicate).
                    "session_id": session_id,
                },
                timeout=120.0,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            logger.exception("worker store-solution failed")
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        db.set_feedback_status(session_id, "accepted")
        # Invalidate the knowledge-graph cache: a new accepted solution
        # changes both the node count and (potentially) the similarity edges.
        _kg_cache.update({"key": None, "value": None, "expires": 0.0})
        return {"status": "accepted", "worker": r.json()}

    db.set_feedback_status(session_id, "rejected")
    return {"status": "rejected"}


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    rows = db.list_session_records(limit=1000)
    by_error: dict[str, int] = {}
    for row in rows:
        cls = _extract_error_class(row)
        by_error[cls] = by_error.get(cls, 0) + 1

    feedback_rows = db.list_feedback_totals()
    by_feedback: dict[str, int] = {}
    for item in feedback_rows:
        status = str(item.get("status") or "new").lower()
        by_feedback[status] = int(item.get("count") or 0)

    return {
        "total": len(rows),
        "by_error_class": dict(sorted(by_error.items(), key=lambda kv: kv[1], reverse=True)),
        "by_feedback_status": by_feedback,
    }


@app.get("/api/results")
def list_results(
    limit: int = Query(default=50, ge=1, le=200),
    job: str | None = Query(default=None),
    error_class: str | None = Query(default=None),
) -> dict[str, Any]:
    rows = db.list_session_records(limit=limit, job=job)
    results = [_to_result_item(dict(r)) for r in rows]
    if error_class:
        wanted = error_class.strip().lower()
        results = [r for r in results if str(r.get("error_class", "")).lower() == wanted]
    return {"count": len(results), "results": _json_safe(results)}


@app.get("/api/results/{run_id}")
def get_result_by_run_id(run_id: str) -> dict[str, Any]:
    row = db.fetch_session(run_id)
    if not row:
        return {"count": 0, "results": []}
    return {"count": 1, "results": [_json_safe(_to_result_item(dict(row)))]}


# ── Knowledge graph (proxied from worker, enriched from Postgres) ──────────
#
# We TTL-cache the worker response in-process so opening the dashboard's
# Knowledge Map tab doesn't pound /knowledge-graph on every refresh. The
# graph itself doesn't change between accepts, so a 60s window is safe.

_KG_CACHE_TTL_S = 60.0
_kg_cache: dict[str, Any] = {"key": None, "value": None, "expires": 0.0}


def _kg_cache_key(limit: int, similarity: float, max_neighbours: int) -> str:
    return f"{limit}|{similarity:.4f}|{max_neighbours}"


def _enrich_knowledge_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Merge Postgres counts into the worker graph payload.

    * Per-solution: ``pg_session_count`` (sessions sharing this fingerprint).
    * Top-level stats: KB-coverage / reuse counts derived from
      ``analysis_sessions``.
    """
    nodes = graph.get("nodes") or []
    try:
        fp_counts = db.fingerprint_session_counts()
    except Exception:
        logger.warning("knowledge-graph: fingerprint_session_counts failed", exc_info=True)
        fp_counts = {}
    try:
        reuse = db.reuse_summary()
    except Exception:
        logger.warning("knowledge-graph: reuse_summary failed", exc_info=True)
        reuse = {"accepted_sessions": 0, "matched_sessions": 0, "total_sessions": 0}

    for node in nodes:
        if node.get("kind") != "solution":
            continue
        fp = str(node.get("fingerprint_text") or "")
        node["pg_session_count"] = int(fp_counts.get(fp, 0)) if fp else 0

    stats = graph.setdefault("stats", {})
    accepted = int(reuse.get("accepted_sessions") or 0)
    total_solutions = int(stats.get("total_solutions") or 0)
    coverage = (total_solutions / accepted) if accepted > 0 else 0.0
    stats.update(
        {
            "accepted_sessions": accepted,
            "matched_sessions": int(reuse.get("matched_sessions") or 0),
            "total_sessions": int(reuse.get("total_sessions") or 0),
            "kb_coverage": round(min(1.0, coverage), 3),
        },
    )
    return graph


@app.get("/api/knowledge-graph")
def get_knowledge_graph(
    limit: int = Query(default=2000, ge=1, le=10_000),
    similarity: float = Query(default=0.0, ge=0.0, le=1.0),
    max_neighbours: int = Query(default=6, ge=1, le=25),
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """Return the typed knowledge graph + KB metrics.

    Backed by the worker's ``/knowledge-graph`` endpoint with a small in-process
    TTL so dashboard refreshes don't slam Elasticsearch. Pass ``refresh=true``
    to bypass the cache (useful right after an Accept).
    """
    key = _kg_cache_key(limit, similarity, max_neighbours)
    now = time.monotonic()
    if (
        not refresh
        and _kg_cache.get("key") == key
        and _kg_cache.get("value") is not None
        and now < float(_kg_cache.get("expires") or 0.0)
    ):
        return _kg_cache["value"]  # type: ignore[no-any-return]

    url = f"{settings.worker_base_url.rstrip('/')}/knowledge-graph"
    params = {
        "limit": limit,
        "similarity": similarity,
        "max_neighbours": max_neighbours,
    }
    try:
        r = httpx.get(
            url,
            headers={"X-Api-Key": settings.worker_api_key},
            params=params,
            timeout=60.0,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "").strip()
        logger.error("worker knowledge-graph HTTP %s: %s", exc.response.status_code, body)
        raise HTTPException(status_code=502, detail=body or str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.exception("worker knowledge-graph fetch failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    enriched = _enrich_knowledge_graph(r.json() or {})
    _kg_cache.update(
        {"key": key, "value": enriched, "expires": now + _KG_CACHE_TTL_S},
    )
    return enriched


# ── Filter configuration (proxied from worker) ─────────────────────────────
#
# The worker is the source of truth for which detectors are loaded and
# which knobs are active. We TTL-cache the response so the Settings page
# can be opened repeatedly without re-hitting the worker — the config
# only changes on worker restart, so a 60s window is conservative.

_FILTER_CONFIG_CACHE_TTL_S = 60.0
_filter_config_cache: dict[str, Any] = {"value": None, "expires": 0.0}


@app.get("/api/filter-config")
def get_filter_config(
    refresh: bool = Query(default=False),
) -> dict[str, Any]:
    """Return the worker's live filter configuration.

    Powers the platform's *Settings → Log Filter* page. Read-only by
    design: changing values still requires editing ``.env`` and restarting
    the worker. Pass ``refresh=true`` to bypass the cache.
    """
    now = time.monotonic()
    cached = _filter_config_cache.get("value")
    expires = float(_filter_config_cache.get("expires") or 0.0)
    if not refresh and cached is not None and now < expires:
        return cached  # type: ignore[no-any-return]

    url = f"{settings.worker_base_url.rstrip('/')}/filter-config"
    try:
        r = httpx.get(
            url,
            headers={"X-Api-Key": settings.worker_api_key},
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = (exc.response.text or "").strip()
        logger.error("worker filter-config HTTP %s: %s", exc.response.status_code, body)
        raise HTTPException(status_code=502, detail=body or str(exc)) from exc
    except httpx.HTTPError as exc:
        logger.exception("worker filter-config fetch failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    payload = r.json() or {}
    _filter_config_cache.update(
        {"value": payload, "expires": now + _FILTER_CONFIG_CACHE_TTL_S},
    )
    return payload


@app.post("/api/listener/poll-once")
def trigger_listener_poll() -> dict[str, Any]:
    try:
        r = httpx.post(
            f"{settings.listener_base_url.rstrip('/')}/poll-once",
            timeout=60.0,
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.exception("listener poll proxy failed")
        detail: Any
        try:
            raw = exc.response.json()
            if isinstance(raw, dict) and "detail" in raw:
                detail = raw["detail"]
            else:
                detail = raw
        except Exception:
            detail = (exc.response.text or "").strip() or str(exc)
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:
        logger.exception("listener poll proxy failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    payload = r.json() if r.content else {}
    return {"status": "ok", "listener": payload}


@app.post("/agent/chat")
def agent_chat(body: AgentChatIn) -> dict[str, Any]:
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    run_id = body.run_id.strip()
    row: dict[str, Any] | None = None
    if run_id:
        fetched = db.fetch_session(run_id)
        if not fetched:
            raise HTTPException(status_code=404, detail="session not found")
        row = dict(fetched)
        db.insert_message(run_id, "user", message)
        msgs = db.list_messages(run_id)
        payload = [{"role": m["role"], "content": m["content"]} for m in msgs]
        answer = _worker_chat(payload, row, use_full_log=body.use_full_log)
        db.insert_message(run_id, "assistant", answer)
    else:
        answer = _worker_chat(
            [{"role": "user", "content": message}],
            None,
            use_full_log=body.use_full_log,
        )

    source_counts = {
        "ci_logs": 1 if row and row.get("filtered_logs") else 0,
        "analyses": 1 if row and row.get("analysis") else 0,
        "solutions": 1 if row and row.get("suggested_fix") else 0,
    }
    return {"answer": answer, "sources": source_counts}

