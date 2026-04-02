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

    @staticmethod
    def _job_path(job_full_name: str) -> str:
        return "/".join([f"job/{quote(part, safe='')}" for part in job_full_name.split("/")])

    def list_failed_builds_from_rss(self, lookback_minutes: int) -> list[dict]:
        url = f"{self.base_url}{self.failed_rss_path}"
        response = self.client.get(url)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        cutoff = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
        items: list[dict] = []
        # Jenkins may return RSS 2.0 (channel/item) or Atom (feed/entry).
        if root.tag.endswith("feed"):
            atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("./atom:entry", atom_ns):
                title = (entry.findtext("atom:title", default="", namespaces=atom_ns) or "").strip()
                link_el = entry.find("atom:link", atom_ns)
                link = (link_el.get("href", "") if link_el is not None else "").strip()
                published_raw = (entry.findtext("atom:published", default="", namespaces=atom_ns) or "").strip()
                if not title or not link:
                    continue

                pub_date = self._parse_datetime(published_raw)
                if pub_date < cutoff:
                    continue

                parsed = self._parse_title_and_link(title, link)
                if parsed:
                    parsed["published_at"] = pub_date.isoformat()
                    items.append(parsed)
        else:
            for item in root.findall("./channel/item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date_raw = (item.findtext("pubDate") or "").strip()
                if not title or not link:
                    continue

                pub_date = self._parse_datetime(pub_date_raw)
                if pub_date < cutoff:
                    continue

                parsed = self._parse_title_and_link(title, link)
                if parsed:
                    parsed["published_at"] = pub_date.isoformat()
                    items.append(parsed)
        return items

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        if not value:
            return datetime.now(UTC)
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(value, fmt)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            except ValueError:
                continue
        return datetime.now(UTC)

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
        job_path = self._job_path(job_full_name)
        url = f"{self.base_url}/{job_path}/{build_number}/api/json"
        response = self.client.get(url, params={"tree": "result,fullDisplayName,url,timestamp"})
        response.raise_for_status()
        return response.json()

    def failed_stages(self, job_full_name: str, build_number: int) -> list[FailedStage]:
        job_path = self._job_path(job_full_name)
        url = f"{self.base_url}/{job_path}/{build_number}/wfapi/describe"
        response = self.client.get(url)
        if response.status_code >= 400:
            return []

        data = response.json()
        stages = data.get("stages", [])
        failed: list[FailedStage] = []
        console_text: str | None = None

        for stage in stages:
            status = str(stage.get("status", "")).upper()
            if status not in {"FAILED", "ERROR", "ABORTED"}:
                continue
            stage_id = str(stage.get("id")) if stage.get("id") is not None else None
            stage_name = str(stage.get("name", "unknown-stage"))
            excerpt = self._stage_log_excerpt(job_full_name, build_number, stage_id, stage_name)
            if not excerpt:
                if console_text is None:
                    console_text = self._build_console_text(job_full_name, build_number)
                excerpt = self._extract_stage_excerpt_from_console(console_text, stage_name)
            failed.append(
                FailedStage(
                    stage_name=stage_name,
                    stage_id=stage_id,
                    status=status,
                    log_excerpt=excerpt,
                )
            )
        return failed

    def _stage_log_excerpt(
        self, job_full_name: str, build_number: int, stage_id: str | None, stage_name: str
    ) -> str:
        if not stage_id:
            return ""
        job_path = self._job_path(job_full_name)

        # 1) Preferred endpoint: wfapi node log
        wfapi_log_url = f"{self.base_url}/{job_path}/{build_number}/execution/node/{stage_id}/wfapi/log"
        wfapi_response = self.client.get(wfapi_log_url)
        if wfapi_response.status_code < 400:
            wfapi_data = wfapi_response.json()
            wfapi_text = str(wfapi_data.get("text", "")).strip()
            if wfapi_text:
                return self._smart_excerpt(wfapi_text, stage_name)

        # 2) Fallback endpoint: classic node raw log
        raw_log_url = f"{self.base_url}/{job_path}/{build_number}/execution/node/{stage_id}/log/?start=0"
        raw_response = self.client.get(raw_log_url)
        if raw_response.status_code < 400:
            raw_text = raw_response.text.strip()
            if raw_text and "not found" not in raw_text.lower():
                return self._smart_excerpt(raw_text, stage_name)

        # 3) Final fallback handled by caller via consoleText extraction.
        return ""

    def _build_console_text(self, job_full_name: str, build_number: int) -> str:
        job_path = self._job_path(job_full_name)
        url = f"{self.base_url}/{job_path}/{build_number}/consoleText"
        response = self.client.get(url)
        if response.status_code >= 400:
            return ""
        return response.text or ""

    def _extract_stage_excerpt_from_console(self, console_text: str, stage_name: str) -> str:
        if not console_text:
            return ""

        lines = console_text.splitlines()
        if not lines:
            return ""

        marker = f"({stage_name})"
        indices = [idx for idx, line in enumerate(lines) if marker in line or stage_name in line]
        if not indices:
            tail = "\n".join(lines[-120:])
            return self._smart_excerpt(tail, stage_name)

        # Use the last stage marker; failures are often near the end.
        center = indices[-1]
        start = max(0, center - 30)
        end = min(len(lines), center + 120)
        window = "\n".join(lines[start:end])
        return self._smart_excerpt(window, stage_name)

    def _smart_excerpt(self, text: str, stage_name: str) -> str:
        lines = text.splitlines()
        if not lines:
            return ""

        max_lines = 220
        head = lines[:40]
        tail = lines[-100:]
        error_pattern = re.compile(
            r"(error|exception|traceback|caused by|failed|exit code|abort|timed out)",
            re.IGNORECASE,
        )
        marker_pattern = re.compile(re.escape(stage_name), re.IGNORECASE)

        highlights: list[str] = []
        for idx, line in enumerate(lines):
            if error_pattern.search(line) or marker_pattern.search(line):
                start = max(0, idx - 3)
                end = min(len(lines), idx + 4)
                highlights.extend(lines[start:end])

        combined: list[str] = []
        combined.extend(head)
        combined.append("... [highlighted snippets] ...")
        combined.extend(highlights[:120])
        combined.append("... [tail] ...")
        combined.extend(tail)

        # De-duplicate while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for line in combined:
            key = line.rstrip()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(line)

        excerpt = "\n".join(deduped[:max_lines]).strip()
        return excerpt[-self.max_stage_log_chars :]

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
