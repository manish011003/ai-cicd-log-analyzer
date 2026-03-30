import logging

from fastapi import FastAPI

from app.checkpoint_store import CheckpointStore
from app.config import settings
from app.dispatcher import WorkerDispatcher
from app.jenkins_client import JenkinsClient
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
)
dispatcher = WorkerDispatcher(
    worker_ingest_url=settings.worker_ingest_url,
    worker_ingest_api_key=settings.worker_ingest_api_key,
    timeout_seconds=settings.request_timeout_seconds,
)
checkpoint_store = CheckpointStore(settings.checkpoint_file)
service = FailureMonitorService(
    jenkins_client=jenkins_client,
    dispatcher=dispatcher,
    checkpoint_store=checkpoint_store,
    initial_lookback_minutes=settings.initial_lookback_minutes,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/poll-once")
def poll_once() -> dict:
    return service.poll_once()
