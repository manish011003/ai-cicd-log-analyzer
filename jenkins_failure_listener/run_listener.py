import logging
import time

from app.checkpoint_store import CheckpointStore
from app.config import settings
from app.dispatcher import WorkerDispatcher
from app.jenkins_client import JenkinsClient
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

    logger.info("Starting Jenkins failure monitor. Poll interval=%ss", settings.poll_interval_seconds)
    while True:
        try:
            result = service.poll_once()
            logger.info("Poll complete: %s", result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Polling failed: %s", exc)
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
