import httpx

from app.models import FailureEvent


class WorkerDispatcher:
    def __init__(self, worker_ingest_url: str, worker_ingest_api_key: str, timeout_seconds: int = 30) -> None:
        self.worker_ingest_url = worker_ingest_url
        self.client = httpx.Client(timeout=timeout_seconds)
        self.headers = {
            "Content-Type": "application/json",
            "X-Api-Key": worker_ingest_api_key,
        }

    def send_failure_event(self, event: FailureEvent) -> None:
        response = self.client.post(
            self.worker_ingest_url,
            headers=self.headers,
            json=event.model_dump(mode="json"),
        )
        response.raise_for_status()
