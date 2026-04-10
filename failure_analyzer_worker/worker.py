"""FastAPI worker that receives Jenkins failure events from the listener
(jenkins_failure_listener), runs them through the LangGraph analysis
pipeline, and returns results.

Integration
───────────
The listener's WorkerDispatcher POSTs to this worker at /ingest/failure.
  Listener config (.env):
      WORKER_INGEST_URL = http://localhost:8090/ingest/failure
      WORKER_INGEST_API_KEY = replace-me
  Worker config (.env):
      WORKER_PORT = 8090
      WORKER_API_KEY = replace-me          ← must match listener key

Endpoints
─────────
POST /ingest/failure   – receive failure event(s) from the listener
POST /store-solution   – store a verified solution back into ES
GET  /health           – liveness check
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import log_processor
from .config import settings
from .graph import analysis_graph

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Failure Analyzer Worker")


# ===================================================================
# Payload models – mirrors the listener's FailureEvent / FailedStage
# (see jenkins_failure_listener/app/models.py)
# ===================================================================

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


# ===================================================================
# Startup – log the listener ↔ worker link, ensure ES index
# ===================================================================

@app.on_event("startup")
async def _startup() -> None:
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
        log_processor.ensure_index()
        logger.info("ES index ready: %s", settings.elasticsearch_index)
    except Exception:
        logger.warning(
            "ES index creation skipped (ES may not be reachable yet)",
            exc_info=True,
        )


# ===================================================================
# Auth – must match listener's WORKER_INGEST_API_KEY
# ===================================================================

def _verify_api_key(key: str) -> None:
    if settings.worker_api_key and key != settings.worker_api_key:
        raise HTTPException(status_code=401, detail="unauthorized")


# ===================================================================
# Routes
# ===================================================================

@app.post("/ingest/failure")
async def ingest_failure(
    body: dict[str, Any],
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    """Accept one event or a batch (``{"failures": [...]}``) from the listener.

    The listener's WorkerDispatcher sends:
      batch  (WORKER_SEND_BATCH=True, default): {"failures": [event, ...]}
      single (WORKER_SEND_BATCH=False):          {event fields directly}
    """
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

            state = await analysis_graph.ainvoke({
                "raw_logs": stage.log_excerpt,
                "stage_name": stage.stage_name,
                "job_name": event.job_full_name,
                "build_number": event.build_number,
                "build_url": event.build_url,
            })

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
            })

    return {"status": "analyzed", "count": len(all_results), "results": all_results}


@app.post("/store-solution")
async def store_solution(
    req: StoreSolutionRequest,
    x_api_key: str = Header(default=""),
) -> dict[str, Any]:
    """Store a verified solution in ES so future builds can find it."""
    _verify_api_key(x_api_key)
    doc_id = log_processor.store_solution(
        fingerprint=req.fingerprint,
        solution=req.solution,
        job_name=req.job_name,
        stage_name=req.stage_name,
        build_number=req.build_number,
    )
    return {"status": "stored", "doc_id": doc_id}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "listener_link": f"http://{settings.worker_host}:{settings.worker_port}/ingest/failure",
        "elasticsearch_url": settings.elasticsearch_url,
        "elasticsearch_index": settings.elasticsearch_index,
        "llm_model": settings.llm_model,
        "embedding_model": settings.embedding_model,
    }
