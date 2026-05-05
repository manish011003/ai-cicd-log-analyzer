"""FastAPI worker: ingest Jenkins failures, run the analysis graph, optionally store solutions.

Wiring model:

  FastAPI lifespan → :func:`failure_analyzer_worker.deps.build_deps` → ``app.state.deps``
                   → :func:`failure_analyzer_worker.graph.build_graph`  → ``app.state.graph``

Routes pull their collaborators from ``request.app.state`` via the small
``_deps`` / ``_graph`` helpers below. Nothing is a module-level singleton,
so tests can override any provider by assigning directly to ``app.state``.
"""

from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .config import settings
from .deps import Deps, build_deps
from .graph import AnalysisGraph, build_graph
from .vectorstore.base import Solution

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Request/response models ──────────────────────────────────────────────────


class FailedStagePayload(BaseModel):
    stage_name: str
    stage_id: str | None = None
    status: str = "FAILED"
    log_excerpt: str = ""


class FailureEventPayload(BaseModel):
    event_type: str = "stage_failure"
    jenkins_url: str = ""
    job_full_name: str
    build_number: int
    build_url: str = ""
    build_result: str = ""
    timestamp: str = ""
    correlation_id: str = ""
    failed_stages: list[FailedStagePayload] = Field(default_factory=list)


class StoreSolutionRequest(BaseModel):
    fingerprint: str
    solution: str
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    solution_score: float = 1.0
    # Optional Postgres session id — if present, becomes the deterministic ES
    # doc id so re-storing the same accepted session overwrites instead of
    # creating a duplicate kNN entry.
    session_id: str = ""


class ChatTurnRequest(BaseModel):
    messages: list[dict[str, str]]
    fingerprint: str = ""
    log_excerpt: str = ""
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    analysis: str = ""
    suggested_fix: str = ""
    # Switch between the compact filtered excerpt (default) and a larger
    # cap so the LLM sees more raw context on demand. The caller is
    # responsible for putting the *right* text in ``log_excerpt`` — the
    # worker only controls truncation.
    use_full_log: bool = False


# ── Lifespan / composition root ──────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Worker starting  port=%s  api_key_set=%s  llm_provider=%s  vector_store=%s",
        settings.worker_port,
        bool(settings.worker_api_key),
        settings.llm_provider,
        settings.vector_store_provider,
    )
    logger.info(
        "Expecting events from jenkins_failure_listener "
        "(listener WORKER_INGEST_URL should be http://%s:%s/ingest/failure)",
        settings.worker_host,
        settings.worker_port,
    )

    deps: Deps = build_deps(settings)
    graph: AnalysisGraph = build_graph(deps)
    app.state.deps = deps
    app.state.graph = graph

    try:
        deps.solutions.ensure_ready()
        deps.solutions.prune(settings.elasticsearch_retention_solutions_days)
        logger.info(
            "Solution repo ready  provider=%s  url=%s  index=%s",
            settings.vector_store_provider,
            settings.elasticsearch_url,
            settings.elasticsearch_index,
        )
    except Exception:
        logger.warning(
            "Vector store readiness skipped (backend may not be reachable yet)",
            exc_info=True,
        )
    yield


app = FastAPI(title="Failure Analyzer Worker", lifespan=lifespan)


def _deps(request: Request) -> Deps:
    return request.app.state.deps  # type: ignore[no-any-return]


def _graph(request: Request) -> AnalysisGraph:
    return request.app.state.graph  # type: ignore[no-any-return]


# ── Security ─────────────────────────────────────────────────────────────────


def _verify_api_key(key: str) -> None:
    if settings.worker_api_key and key != settings.worker_api_key:
        raise HTTPException(status_code=401, detail="unauthorized")


# ── Web UI session hook ──────────────────────────────────────────────────────


def _register_web_sessions(rows: list[dict[str, Any]]) -> None:
    base = (settings.web_ui_api_url or "").strip().rstrip("/")
    if not base or not rows:
        return
    pub = (settings.web_ui_public_url or "").strip().rstrip("/") or "http://127.0.0.1:3000"
    post_url = f"{base}/api/sessions"
    timeout = settings.web_ui_session_timeout_seconds
    with httpx.Client(timeout=timeout) as client:
        for row in rows:
            body = {
                "job_full_name": row.get("job_name") or "",
                "build_number": int(row.get("build_number") or 0),
                "stage_name": row.get("stage_name") or "",
                "build_url": row.get("build_url") or "",
                "fingerprint": row.get("fingerprint") or "",
                "analysis": row.get("analysis") or "",
                "suggested_fix": row.get("suggested_fix") or "",
                "filtered_logs": row.get("filtered_logs") or "",
                "raw_logs": row.get("raw_logs") or "",
                "matched_solution": row.get("matched_solution") or "",
                "match_score": float(row.get("match_score") or 0),
                "recommendation": row.get("recommendation") or "",
                "similar_past": row.get("similar_past") or [],
            }
            try:
                r = client.post(post_url, json=body)
                r.raise_for_status()
                data = r.json()
                sid = data.get("id")
                if sid:
                    row["web_session_id"] = sid
                    row["web_session_url"] = f"{pub}/?session={sid}"
                    logger.info("Web UI session: %s", row["web_session_url"])
            except Exception:
                logger.warning("Web UI session hook failed", exc_info=True)


# ── Routes ───────────────────────────────────────────────────────────────────


@app.post("/ingest/failure")
def ingest_failure(
    body: dict[str, Any],
    request: Request,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    """One event or ``{"failures": [...]}`` batch from the listener."""
    _verify_api_key(x_api_key)

    if "failures" in body and isinstance(body["failures"], list):
        events = [FailureEventPayload(**f) for f in body["failures"]]
    else:
        events = [FailureEventPayload(**body)]

    logger.info(
        "Received %d event(s) from listener: %s",
        len(events),
        [(e.job_full_name, e.build_number, len(e.failed_stages)) for e in events],
    )

    graph = _graph(request)
    all_results: list[dict[str, Any]] = []

    for event in events:
        for stage in event.failed_stages:
            if not stage.log_excerpt.strip():
                continue

            logger.info(
                "Analyzing %s #%d stage=%s  excerpt_len=%d",
                event.job_full_name,
                event.build_number,
                stage.stage_name,
                len(stage.log_excerpt),
            )

            state = graph.invoke({
                "raw_logs": stage.log_excerpt,
                "stage_name": stage.stage_name,
                "job_name": event.job_full_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
            })

            similar = state.get("es_matches") or []
            # Cap raw_logs so we don't blow up Postgres rows on pathological
            # builds (10MB+ logs). The listener already crops to ~50KB, but we
            # hard-stop here regardless.
            raw_cap = settings.raw_log_max_chars
            raw_logs = stage.log_excerpt[:raw_cap] if raw_cap > 0 else stage.log_excerpt
            all_results.append({
                "job_name": event.job_full_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "correlation_id": event.correlation_id,
                "stage_name": stage.stage_name,
                "fingerprint": state.get("fingerprint", ""),
                "filtered_logs": state.get("filtered_logs", ""),
                "raw_logs": raw_logs,
                "analysis": state.get("analysis", ""),
                "suggested_fix": state.get("suggested_fix", ""),
                "recommendation": state.get("recommendation", ""),
                "match_score": state.get("match_score", 0),
                "matched_solution": state.get("matched_solution", ""),
                "similar_past": [
                    {
                        "score": m.get("score", 0.0),
                        "solution": m.get("solution", ""),
                        "fingerprint_text": m.get("fingerprint_text", ""),
                    }
                    for m in similar
                ],
            })

    _register_web_sessions(all_results)

    return {"status": "analyzed", "count": len(all_results), "results": all_results}


@app.post("/store-solution")
def store_solution(
    req: StoreSolutionRequest,
    request: Request,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    """Persist a verified solution via the configured vector store.

    Idempotency: when ``session_id`` is supplied, we use it (lowercased) as
    the ES doc id. ES's ``index`` API treats this as upsert-by-id, so a
    second Accept for the same session overwrites the existing kNN entry
    instead of creating a duplicate. When no session_id is supplied we
    fall back to a content hash of (job, stage, build, fingerprint) so even
    legacy callers get dedup.
    """
    _verify_api_key(x_api_key)
    deps = _deps(request)
    try:
        deps.solutions.ensure_ready()
        doc_id = deps.solutions.store(
            Solution(
                fingerprint_text=req.fingerprint,
                solution=req.solution,
                job_name=req.job_name,
                stage_name=req.stage_name,
                build_number=req.build_number,
                solution_score=req.solution_score,
            ),
            doc_id=_solution_doc_id(req),
        )
    except Exception as exc:
        logger.exception("store_solution failed")
        raise HTTPException(
            status_code=500, detail=f"vector store store failed: {str(exc)[:300]}",
        ) from exc
    return {"status": "stored", "doc_id": doc_id}


def _solution_doc_id(req: "StoreSolutionRequest") -> str:
    """Pick a stable doc id for the kNN store.

    Prefer the Postgres session id (one accepted solution per session row).
    If absent — e.g. a script calling /store-solution directly — derive a
    content hash so re-runs of the same script don't pollute the index.
    """
    sid = (req.session_id or "").strip().lower()
    if sid:
        return sid
    fingerprint = (req.fingerprint or "").strip()
    seed = f"{req.job_name}|{req.build_number}|{req.stage_name}|{fingerprint}".lower()
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@app.post("/chat/turn")
def chat_turn(
    req: ChatTurnRequest,
    request: Request,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    _verify_api_key(x_api_key)
    graph = _graph(request)
    try:
        text = graph.chat_clarification(
            req.messages,
            fingerprint=req.fingerprint,
            log_excerpt=req.log_excerpt,
            job_name=req.job_name,
            stage_name=req.stage_name,
            build_number=req.build_number,
            analysis=req.analysis,
            suggested_fix=req.suggested_fix,
            use_full_log=req.use_full_log,
        )
    except Exception as exc:
        logger.exception("chat_clarification failed")
        msg = str(exc)
        if "rate_limit" in msg.lower() or "429" in msg:
            raise HTTPException(
                status_code=429,
                detail="LLM rate limit reached. Please wait a few minutes and try again.",
            ) from exc
        raise HTTPException(
            status_code=500, detail=f"LLM chat turn failed: {msg[:300]}",
        ) from exc
    return {"role": "assistant", "content": text}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "listener_link": f"http://{settings.worker_host}:{settings.worker_port}/ingest/failure",
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "vector_store_provider": settings.vector_store_provider,
        "elasticsearch_url": settings.elasticsearch_url,
        "elasticsearch_index": settings.elasticsearch_index,
        "retention_solutions_days": settings.elasticsearch_retention_solutions_days,
        "web_ui_api_url": settings.web_ui_api_url or None,
    }
