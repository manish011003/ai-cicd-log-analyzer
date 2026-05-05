import logging
import time

from app.ci import create_ci_source
from app.config import settings
from app.dispatcher import WorkerDispatcher
from app.postgres_state_store import PostgresStateStore
from app.service import FailureMonitorService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    ci_source = create_ci_source(settings)
    dispatcher = WorkerDispatcher(
        worker_ingest_url=settings.worker_ingest_url,
        worker_ingest_api_key=settings.worker_ingest_api_key,
        timeout_seconds=settings.request_timeout_seconds,
        send_batch=settings.worker_send_batch,
    )
    state_store = PostgresStateStore(settings.database_url)
    service = FailureMonitorService(
        ci_source=ci_source,
        dispatcher=dispatcher,
        state_store=state_store,
    )

    logger.info(
        "Starting CI failure monitor. provider=%s poll_interval=%ss",
        settings.ci_provider,
        settings.poll_interval_seconds,
    )
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
