import logging
from datetime import UTC, datetime, timedelta

from app.dispatcher import WorkerDispatcher
from app.jenkins_client import JenkinsClient
from app.postgres_state_store import PostgresStateStore

logger = logging.getLogger(__name__)


class FailureMonitorService:
    def __init__(
        self,
        jenkins_client: JenkinsClient,
        dispatcher: WorkerDispatcher,
        state_store: PostgresStateStore,
        initial_lookback_minutes: int,
    ) -> None:
        self.jenkins_client = jenkins_client
        self.dispatcher = dispatcher
        self.state_store = state_store
        self.initial_lookback_minutes = initial_lookback_minutes
        self.started_at = datetime.now(UTC)

    def poll_once(self) -> dict[str, int]:
        lookback = self._current_lookback_minutes()
        failed = self.jenkins_client.list_failed_builds_from_rss(lookback_minutes=lookback)
        processed = 0
        forwarded = 0

        # Oldest first for determinism.
        failed.sort(key=lambda item: (item["job_full_name"], item["build_number"]))

        for item in failed:
            job = item["job_full_name"]
            number = item["build_number"]
            build_url = item["build_url"]
            if not self.state_store.should_process(job, number):
                continue

            try:
                processed += 1
                event = self.jenkins_client.build_failure_event(job, number, build_url)
                self.dispatcher.send_failure_event(event)
                forwarded += 1
                self.state_store.mark_processed(job, number)
                logger.info("Forwarded failure event: %s #%s (%s)", job, number, event.event_type)
            except Exception as exc:  # noqa: BLE001
                self.state_store.mark_failed(job, number, str(exc))
                raise

        return {"processed": processed, "forwarded": forwarded}

    def _current_lookback_minutes(self) -> int:
        # Use a wider lookback only right after startup to avoid missing fresh failures.
        warmup_window = timedelta(minutes=self.initial_lookback_minutes)
        if datetime.now(UTC) - self.started_at < warmup_window:
            return self.initial_lookback_minutes
        return 5
