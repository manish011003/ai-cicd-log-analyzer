import logging

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder

from app.config import settings
from app.dispatcher import WorkerDispatcher
from app.jenkins_client import JenkinsClient
from app.postgres_state_store import PostgresStateStore
from app.service import FailureMonitorService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Jenkins Failure Listener", version="1.0.0")

jenkins_client = JenkinsClient(
    base_url=settings.jenkins_base_url,
    user=settings.jenkins_user,
    api_token=settings.jenkins_api_token,
    failed_rss_path=settings.jenkins_failed_rss_path,
    timeout_seconds=settings.request_timeout_seconds,
    max_stage_log_chars=settings.max_stage_log_chars,
    max_stage_scan_lines=settings.max_stage_scan_lines,
    per_error_context_before=settings.per_error_context_before,
    per_error_context_after=settings.per_error_context_after,
    error_anchor_merge_gap_lines=settings.error_anchor_merge_gap_lines,
    max_error_regions_per_stage=settings.max_error_regions_per_stage,
    min_anchor_score_for_snippet=settings.min_anchor_score_for_snippet,
    parallel_stage_overlap_ms=settings.parallel_stage_overlap_ms,
)
dispatcher = WorkerDispatcher(
    worker_ingest_url=settings.worker_ingest_url,
    worker_ingest_api_key=settings.worker_ingest_api_key,
    timeout_seconds=settings.request_timeout_seconds,
    send_batch=settings.worker_send_batch,
)
state_store = PostgresStateStore(settings.database_url)
service = FailureMonitorService(
    jenkins_client=jenkins_client,
    dispatcher=dispatcher,
    state_store=state_store,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _safe_http_error_body(exc: httpx.HTTPStatusError) -> str:
    """Avoid secondary failures when building error detail (encoding / huge bodies)."""
    try:
        raw = exc.response.content or b""
        return raw.decode("utf-8", errors="replace")[:4000]
    except Exception:
        return "<could not read response body>"


@app.post("/poll-once")
def poll_once() -> dict:
    try:
        result = service.poll_once()
    except httpx.HTTPStatusError as exc:
        logger.exception("poll_once: upstream HTTP error")
        req_url = str(exc.request.url) if exc.request is not None else ""
        raise HTTPException(
            status_code=502,
            detail={
                "reason": "upstream_http_error",
                "url": req_url,
                "status_code": exc.response.status_code,
                "body_preview": _safe_http_error_body(exc),
            },
        ) from exc
    except httpx.RequestError as exc:
        logger.exception("poll_once: upstream connection error")
        raise HTTPException(
            status_code=502,
            detail={"reason": "upstream_connect_error", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("poll_once failed")
        raise HTTPException(
            status_code=500,
            detail={"reason": "internal_error", "type": type(exc).__name__, "message": str(exc)},
        ) from exc

    try:
        jsonable_encoder(result)
    except Exception as exc:
        logger.exception("poll_once: response not JSON-serializable")
        raise HTTPException(
            status_code=500,
            detail={"reason": "serialization_error", "type": type(exc).__name__, "message": str(exc)},
        ) from exc

    return result


@app.post("/maintenance/purge-state")
def purge_state() -> dict:
    deleted = state_store.purge_processed_older_than_days(settings.state_retention_days)
    return {"deleted": deleted, "retention_days": settings.state_retention_days}
