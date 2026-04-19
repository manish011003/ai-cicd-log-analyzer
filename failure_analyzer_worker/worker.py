"""FastAPI worker: ingest Jenkins failures, run LangGraph analysis, optional ES solution store.

Listener posts to ``POST /ingest/failure``; set ``WORKER_INGEST_URL`` and matching API keys
in listener and worker ``.env`` files. Routes: ``/ingest/failure``, ``/store-solution``, ``/store-context``, ``/chat/turn``, ``/health``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import log_processor
from .config import settings
from .graph import analysis_graph, chat_clarification_reply

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(
        "Worker starting  port=%s  api_key_set=%s",
        settings.worker_port,
        bool(settings.worker_api_key),
    )
    logger.info(
        "Expecting events from jenkins_failure_listener "
        "(listener WORKER_INGEST_URL should be "
        "http://%s:%s/ingest/failure)",
        settings.worker_host,
        settings.worker_port,
    )
    try:
        log_processor.reset_es_client()
        log_processor.ensure_index()
        log_processor.ensure_context_index()
        log_processor.prune_stale_documents()
        logger.info(
            "ES index ready: %s  url=%s",
            settings.elasticsearch_index,
            settings.elasticsearch_url,
        )
    except Exception:
        logger.warning(
            "ES index creation skipped (ES may not be reachable yet)",
            exc_info=True,
        )
    yield


app = FastAPI(title="Failure Analyzer Worker", lifespan=lifespan)


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


class StoreContextRequest(BaseModel):
    fingerprint: str
    filtered_excerpt: str


class ChatTurnRequest(BaseModel):
    messages: list[dict[str, str]]
    fingerprint: str = ""
    log_excerpt: str = ""
    job_name: str = ""
    stage_name: str = ""
    build_number: int = 0
    analysis: str = ""
    suggested_fix: str = ""


def _verify_api_key(key: str) -> None:
    if settings.worker_api_key and key != settings.worker_api_key:
        raise HTTPException(status_code=401, detail="unauthorized")


def _register_web_sessions(rows: list[dict[str, Any]]) -> None:
    base = (settings.web_ui_api_url or "").strip().rstrip("/")
    if not base or not rows:
        return
    pub = (settings.web_ui_public_url or "").strip().rstrip("/") or "http://127.0.0.1:3000"
    post_url = f"{base}/api/sessions"
    with httpx.Client(timeout=20.0) as client:
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


@app.post("/ingest/failure")
def ingest_failure(
    body: dict[str, Any],
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

            state = analysis_graph.invoke({
                "raw_logs": stage.log_excerpt,
                "stage_name": stage.stage_name,
                "job_name": event.job_full_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
            })

            similar = state.get("es_matches") or []
            all_results.append({
                "job_name": event.job_full_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
                "correlation_id": event.correlation_id,
                "stage_name": stage.stage_name,
                "fingerprint": state.get("fingerprint", ""),
                "filtered_logs": state.get("filtered_logs", ""),
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
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    """Persist a verified solution to Elasticsearch."""
    _verify_api_key(x_api_key)
    try:
        log_processor.ensure_index()
        doc_id = log_processor.store_solution(
            fingerprint=req.fingerprint,
            solution=req.solution,
            job_name=req.job_name,
            stage_name=req.stage_name,
            build_number=req.build_number,
            solution_score=req.solution_score,
        )
    except Exception as exc:
        logger.exception("store_solution failed")
        raise HTTPException(status_code=500, detail=f"ES store failed: {str(exc)[:300]}")
    return {"status": "stored", "doc_id": doc_id}


@app.post("/store-context")
def store_context(
    req: StoreContextRequest,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    _verify_api_key(x_api_key)
    try:
        doc_id = log_processor.store_filtered_context(
            req.fingerprint,
            req.filtered_excerpt,
        )
    except Exception as exc:
        logger.exception("store_context failed")
        raise HTTPException(status_code=500, detail=f"ES store failed: {str(exc)[:300]}")
    return {"status": "stored", "doc_id": doc_id}


@app.post("/chat/turn")
def chat_turn(
    req: ChatTurnRequest,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    _verify_api_key(x_api_key)
    try:
        text = chat_clarification_reply(
            req.messages,
            fingerprint=req.fingerprint,
            log_excerpt=req.log_excerpt,
            job_name=req.job_name,
            stage_name=req.stage_name,
            build_number=req.build_number,
            analysis=req.analysis,
            suggested_fix=req.suggested_fix,
        )
    except Exception as exc:
        logger.exception("chat_clarification_reply failed")
        msg = str(exc)
        if "rate_limit" in msg.lower() or "429" in msg:
            raise HTTPException(
                status_code=429,
                detail="LLM rate limit reached. Please wait a few minutes and try again.",
            )
        raise HTTPException(status_code=500, detail=f"LLM chat turn failed: {msg[:300]}")
    return {"role": "assistant", "content": text}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "listener_link": f"http://{settings.worker_host}:{settings.worker_port}/ingest/failure",
        "elasticsearch_url": settings.elasticsearch_url,
        "elasticsearch_index": settings.elasticsearch_index,
        "elasticsearch_context_index": settings.elasticsearch_context_index,
        "retention_solutions_days": settings.elasticsearch_retention_solutions_days,
        "retention_context_days": settings.elasticsearch_retention_context_days,
        "web_ui_api_url": settings.web_ui_api_url or None,
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
    }
