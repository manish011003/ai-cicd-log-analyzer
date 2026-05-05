import logging

from app.ci import CISource
from app.dispatcher import WorkerDispatcher
from app.models import FailureEvent
from app.postgres_state_store import PostgresStateStore

logger = logging.getLogger(__name__)


class FailureMonitorService:
    """Pure polling loop: asks the CI source for new failed builds, records
    claims in Postgres, forwards events to the worker.

    Accepts *any* :class:`CISource` implementation, so adding GitLab /
    GitHub Actions support is a matter of writing a new adapter and
    flipping ``CI_PROVIDER`` in the environment.
    """

    def __init__(
        self,
        ci_source: CISource,
        dispatcher: WorkerDispatcher,
        state_store: PostgresStateStore,
    ) -> None:
        self.ci_source = ci_source
        self.dispatcher = dispatcher
        self.state_store = state_store

    def poll_once(self) -> dict:
        self.state_store.release_stale_processing(stale_minutes=30)

        all_failed = self.ci_source.list_failed_builds()

        jobs_in_feed: set[str] = set()
        for item in all_failed:
            jobs_in_feed.add(item["job_full_name"])

        last_processed: dict[str, int] = {}
        for job in jobs_in_feed:
            last_processed[job] = self.state_store.get_last_processed_build_number(job)

        new_failures = [
            item for item in all_failed
            if item["build_number"] > last_processed.get(item["job_full_name"], 0)
        ]

        new_failures.sort(key=lambda item: (item["job_full_name"], item["build_number"]))

        seen: set[tuple[str, int]] = set()
        deduped: list[dict] = []
        for item in new_failures:
            key = (item["job_full_name"], item["build_number"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        new_failures = deduped

        preview = [(item["job_full_name"], item["build_number"]) for item in new_failures[:25]]
        logger.info(
            "Poll ci_total=%d new_after_db_filter=%d preview=%s",
            len(all_failed),
            len(new_failures),
            preview,
        )

        claimed: list[tuple[str, int, str]] = []
        events: list[FailureEvent] = []

        for item in new_failures:
            job = item["job_full_name"]
            number = item["build_number"]
            build_url = item["build_url"]
            if not self.state_store.claim_for_processing(job, number):
                continue

            try:
                event = self.ci_source.build_failure_event(job, number, build_url)
                events.append(event)
                claimed.append((job, number, build_url))
            except Exception as exc:  # noqa: BLE001
                self.state_store.mark_failed(job, number, str(exc))
                raise

        worker_analysis: dict = {}
        if events:
            if self.dispatcher.send_batch:
                try:
                    worker_analysis = self.dispatcher.send_failure_events(events)
                except Exception as exc:  # noqa: BLE001
                    for job, number, _ in claimed:
                        self.state_store.mark_failed(job, number, f"worker forward: {exc}")
                    raise
                for job, number, _ in claimed:
                    self.state_store.mark_processed(job, number)
            else:
                all_results: list = []
                for event, (job, number, _) in zip(events, claimed, strict=True):
                    try:
                        resp = self.dispatcher.send_failure_events([event])
                    except Exception as exc:  # noqa: BLE001
                        self.state_store.mark_failed(job, number, f"worker forward: {exc}")
                        raise
                    self.state_store.mark_processed(job, number)
                    all_results.extend(resp.get("results", []))
                worker_analysis = {"status": "analyzed", "count": len(all_results), "results": all_results}
            logger.info(
                "Forwarded %d failure(s)%s: %s",
                len(events),
                " in one batch" if self.dispatcher.send_batch else "",
                [(e.job_full_name, e.build_number) for e in events],
            )

        forwarded = len(events)
        return {
            "processed": forwarded,
            "forwarded": forwarded,
            "failures": [e.model_dump(mode="json") for e in events],
            "analysis": worker_analysis,
        }
