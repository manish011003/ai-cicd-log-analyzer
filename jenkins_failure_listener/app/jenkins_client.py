import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.models import FailedStage, FailureEvent


class JenkinsClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        api_token: str,
        failed_rss_path: str,
        timeout_seconds: int = 30,
        max_stage_log_chars: int = 30000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.failed_rss_path = failed_rss_path
        self.max_stage_log_chars = max_stage_log_chars
        self.client = httpx.Client(
            auth=(user, api_token),
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    def list_failed_builds_from_rss(self, lookback_minutes: int) -> list[dict]:
        url = f"{self.base_url}{self.failed_rss_path}"
        response = self.client.get(url)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        cutoff = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
        items: list[dict] = []

        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date_raw = (item.findtext("pubDate") or "").strip()
            if not title or not link:
                continue

            try:
                pub_date = datetime.strptime(pub_date_raw, "%a, %d %b %Y %H:%M:%S %z")
            except ValueError:
                pub_date = datetime.now(UTC)

            if pub_date < cutoff:
                continue

            parsed = self._parse_title_and_link(title, link)
            if parsed:
                items.append(parsed)
        return items

    @staticmethod
    def _parse_title_and_link(title: str, link: str) -> dict | None:
        # Example title: "my-folder » my-job #123 (Failed)"
        match = re.search(r"(?P<job>.+)\s+#(?P<num>\d+)\s+\(", title)
        if not match:
            return None

        job_raw = match.group("job").strip()
        build_number = int(match.group("num"))
        job_full_name = job_raw.replace(" » ", "/")
        return {
            "job_full_name": job_full_name,
            "build_number": build_number,
            "build_url": link.rstrip("/") + "/",
        }

    def build_api(self, job_full_name: str, build_number: int) -> dict:
        job_path = "/".join([f"job/{quote(part, safe='')}" for part in job_full_name.split("/")])
        url = f"{self.base_url}/{job_path}/{build_number}/api/json"
        response = self.client.get(url, params={"tree": "result,fullDisplayName,url,timestamp"})
        response.raise_for_status()
        return response.json()

    def failed_stages(self, job_full_name: str, build_number: int) -> list[FailedStage]:
        job_path = "/".join([f"job/{quote(part, safe='')}" for part in job_full_name.split("/")])
        url = f"{self.base_url}/{job_path}/{build_number}/wfapi/describe"
        response = self.client.get(url)
        if response.status_code >= 400:
            return []

        data = response.json()
        stages = data.get("stages", [])
        failed: list[FailedStage] = []

        for stage in stages:
            status = str(stage.get("status", "")).upper()
            if status not in {"FAILED", "ERROR", "ABORTED"}:
                continue
            stage_id = str(stage.get("id")) if stage.get("id") is not None else None
            stage_name = str(stage.get("name", "unknown-stage"))
            excerpt = self._stage_log_excerpt(job_full_name, build_number, stage_id)
            failed.append(
                FailedStage(
                    stage_name=stage_name,
                    stage_id=stage_id,
                    status=status,
                    log_excerpt=excerpt,
                )
            )
        return failed

    def _stage_log_excerpt(self, job_full_name: str, build_number: int, stage_id: str | None) -> str:
        if not stage_id:
            return ""
        job_path = "/".join([f"job/{quote(part, safe='')}" for part in job_full_name.split("/")])
        url = f"{self.base_url}/{job_path}/{build_number}/execution/node/{stage_id}/wfapi/log"
        response = self.client.get(url)
        if response.status_code >= 400:
            return ""
        text = response.json().get("text", "") or ""
        return text[-self.max_stage_log_chars :]

    def build_failure_event(self, job_full_name: str, build_number: int, build_url: str) -> FailureEvent:
        api_data = self.build_api(job_full_name, build_number)
        failed_stages = self.failed_stages(job_full_name, build_number)
        event_type = "stage_failure" if failed_stages else "build_failure"

        return FailureEvent(
            event_type=event_type,
            jenkins_url=self.base_url,
            job_full_name=job_full_name,
            build_number=build_number,
            build_url=build_url,
            build_result=api_data.get("result", "FAILURE"),
            failed_stages=failed_stages,
            timestamp=datetime.now(UTC),
            correlation_id=str(uuid4()),
        )
