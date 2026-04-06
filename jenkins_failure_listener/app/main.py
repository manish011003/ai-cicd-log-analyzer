import logging

from fastapi import FastAPI

from app.config import settings
from app.dispatcher import WorkerDispatcher
from app.jenkins_client import JenkinsClient
from app.postgres_state_store import PostgresStateStore
from app.service import FailureMonitorService

logging.basicConfig(level=logging.INFO)

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


@app.post("/poll-once")
def poll_once() -> dict:
    return service.poll_once()


@app.post("/maintenance/purge-state")
def purge_state() -> dict:
    deleted = state_store.purge_processed_older_than_days(settings.state_retention_days)
    return {"deleted": deleted, "retention_days": settings.state_retention_days}
