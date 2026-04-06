import logging
import time

from app.config import settings
from app.dispatcher import WorkerDispatcher
from app.jenkins_client import JenkinsClient
from app.postgres_state_store import PostgresStateStore
from app.service import FailureMonitorService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
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

    logger.info("Starting Jenkins failure monitor. Poll interval=%ss", settings.poll_interval_seconds)
    while True:
        try:
            result = service.poll_once()
            summary = {k: v for k, v in result.items() if k != "failures"}
            summary["failure_builds"] = [
                (f["job_full_name"], f["build_number"]) for f in result.get("failures", [])
            ]
            logger.info("Poll complete: %s", summary)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Polling failed: %s", exc)
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
