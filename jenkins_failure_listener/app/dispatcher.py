import httpx

from app.models import FailureEvent


class WorkerDispatcher:
    def __init__(
        self,
        worker_ingest_url: str,
        worker_ingest_api_key: str,
        timeout_seconds: int = 30,
        send_batch: bool = True,
    ) -> None:
        self.worker_ingest_url = worker_ingest_url
        self.send_batch = send_batch
        self.client = httpx.Client(timeout=timeout_seconds)
        self.headers = {
            "Content-Type": "application/json",
            "X-Api-Key": worker_ingest_api_key,
        }

    def send_failure_events(self, events: list[FailureEvent]) -> None:
        if not events:
            return
        if self.send_batch:
            response = self.client.post(
                self.worker_ingest_url,
                headers=self.headers,
                json={"failures": [e.model_dump(mode="json") for e in events]},
            )
            response.raise_for_status()
            return
        for event in events:
            response = self.client.post(
                self.worker_ingest_url,
                headers=self.headers,
                json=event.model_dump(mode="json"),
            )
            response.raise_for_status()
