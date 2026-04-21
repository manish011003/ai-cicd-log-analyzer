import html as html_mod
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.models import FailedStage, FailureEvent

logger = logging.getLogger(__name__)


def _xml_local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _rss_child_text(parent: ET.Element, local: str) -> str:
    for child in parent:
        if _xml_local_name(child.tag) == local:
            return (child.text or "").strip()
    return ""


def _rss_item_link(item: ET.Element) -> str:
    for child in item:
        if _xml_local_name(child.tag) != "link":
            continue
        if (child.text or "").strip():
            return (child.text or "").strip()
        href = child.get("href")
        if href:
            return href.strip()
    return ""


_HTML_TAG = re.compile(r"<[^>]+>")
_JENKINS_TS_PREFIX = re.compile(r"^\d{2}:\d{2}:\d{2}\s+", re.MULTILINE)


def _strip_html(text: str) -> str:
    """Remove HTML tags / entities that Jenkins wfapi injects."""
    text = _HTML_TAG.sub("", text)
    text = html_mod.unescape(text)
    text = _JENKINS_TS_PREFIX.sub("", text)
    return text


@lru_cache(maxsize=1)
def _log_error_pattern_bundle() -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    typed_throwable = re.compile(r"(?i)\b[A-Z][a-zA-Z0-9_]*(?:Error|Exception)\b")
    _throwable_raw = r"\b[A-Z][a-zA-Z0-9_]*(?:Error|Exception)\b"
    failure_signal = re.compile(
        r"(?i)(?:"
        r"\b(?:fatal|failure|failures|failed|failing)\b|"
        r"\b(?:exception|exceptions|traceback)\b|"
        r"\bpanic(?:ked)?\b|"
        r"^\s*Error:\s+|"
        r"\b(?:error|err)\b\s*[:!\[\]]|"
        r"\[(?:ERROR|FATAL|CRITICAL)\]|"
        r"\b(?:denied|forbidden|unauthorized|unauthorised)\b|"
        r"(?:^|\s)(?:exit|return)\s*(?:code|status)?\s*[:=]\s*[1-9]\d*|"
        r"script returned exit code\s*[1-9]|"
        r"\bnon-zero\s+exit\b|"
        r"\bFAILED\b|"
        r"\b(?:ECONNREFUSED|ETIMEDOUT|ENOTFOUND|ECONNRESET)\b|"
        r"\b(?:could not|couldn't|unable to)\s+(?:connect|reach|resolve|open|load|find|start|create)\b|"
        r"connection\s+refused|connection\s+reset|broken\s+pipe|"
        r"timed\s+out\s+waiting|"
        r"\bcaused\s+by\s*:\s*\S+|"
        r"\berror\[[A-Z0-9]+\]|"
        r"\bassertion\s+failed\b|internal\s+compiler\s+error|"
        r"\bsegmentation\s+fault\b|\bcore\s+dumped\b|"
        + _throwable_raw
        + r")",
    )
    boilerplate_anchor = re.compile(
        r"(?i)"
        r"re-run\s+maven|for\s+more\s+information\s+about|^\s*---+\s*$|"
        r"cwiki\.apache\.org|\[help\s*1\]|full\s+debug\s+logging|"
        r"^\s*Downloading\s|^\s*Progress\s*\(|Resolving\s+dependencies|"
        r"^\s*\+\+\+\s|"
        # Spring Cloud / Eureka / Netflix infrastructure retry noise
        r"Request execution error.*endpoint=DefaultEndpoint|"
        r"Request execution failed with message:|"
        r"Cannot execute request on any known server|"
        r"was unable to refresh its cache|"
        r"Initial registry fetch from.*servers failed|"
        r"registration failed Cannot execute|"
        r"ConfigServerConfigDataLoader\s*:|"
        r"Fetching config from server at|"
        r"Exception on Url\s*-\s*http://localhost|"
        r"jakarta\.ws\.rs\.ProcessingException:\s*java\.net\.Connect|"
        r"HttpHostConnectException:\s*Connect to http://localhost|"
        r"TransportException:\s*Cannot execute request|"
        r"ResourceAccessException:.*Connection refused|"
        r"Caused by:\s*java\.net\.ConnectException:\s*Connection refused|"
        r"Caused by:\s*org\.apache\.maven\.plugin\.MojoFailureException|"
        r"LifecycleExecutionException:\s*Failed to execute goal|"
        r"See /var/jenkins_home/.*surefire-reports|"
        r"See dump files \(if any exist\)|"
        r"OptionalValidatorFactoryBean.*no.*provider",
    )
    operational_hint = re.compile(
        r"(?i)\b(?:connection|timeout|timed\s+out|socket|dns|network|unreachable|"
        r"refused|unavailable|not\s+found|no\s+such\s+file|permission\s+denied)\b"
    )
    return typed_throwable, failure_signal, boilerplate_anchor, operational_hint


def _score_line_as_error_anchor(line: str) -> int | None:
    typed_throwable, failure_signal, boilerplate_anchor, operational_hint = _log_error_pattern_bundle()
    raw = line.strip()
    if len(raw) < 4:
        return None
    if boilerplate_anchor.search(line):
        return None
    if not failure_signal.search(line):
        return None
    score = 1
    if typed_throwable.search(line):
        score += 4
    elif re.search(r"(?i)\b(exception|exceptions|traceback|fatal|panic|failed|failure)\b", line):
        score += 3
    if re.search(r"(?i)(?:exit|return)\s*(?:code|status)?\s*[:=]\s*[1-9]", line):
        score += 2
    if re.search(r"(?i)script returned exit code\s*[1-9]", line):
        score += 2
    if re.search(r"(?i)connection\s+refused|ECONNREFUSED|ENOTFOUND|ETIMEDOUT", line):
        score += 3
    if operational_hint.search(line):
        score += 2
    if re.search(r"(?i)https?://", raw) and raw.lower().count("error") < 2:
        score -= 2
    if re.match(r"(?i)\s*at\s+[\w$.]+\(", raw) and not typed_throwable.search(line):
        score -= 2
    return score


# Flexible prefix: zero or more timestamps in [ISO], bare ISO, or HH:MM:SS format.
# Handles consoleText double-timestamps: [2026-04-20T07:34:28.291Z] 2026-04-20T07:34:27.518Z
_TS = r"(?:\[\d{4}-[^\]]+\]\s*|\d{4}-\d{2}-\d{2}T\S+\s+|\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+)*"

_LOG_NOISE_LINE = re.compile(
    rf"(?i)"
    rf"^\s*$|"
    # [Pipeline] boilerplate (with optional leading timestamp for consoleText)
    rf"^\s*{_TS}\[Pipeline\]\s*(?:\{{|\}}|//\s*\w+|$)|"
    rf"^\s*{_TS}\[Pipeline\]\s*(?:stage|node|parallel|withEnv|timestamps|timeout|"
    rf"getContext|End of Pipeline|tool|envVarsForTool)\b|"
    # Maven / Gradle progress lines with no error value
    rf"^\s*{_TS}\[INFO\]\s*[-=]{{4,}}\s*$|"
    rf"^\s*{_TS}\[INFO\]\s*$|"
    rf"^\s*{_TS}\[INFO\]\s*(?:Scanning for projects|"
    rf"Building\s|Compiling\s|Copying\s|Installing\s|Deleting\s|Recompiling\s|"
    rf"Downloaded\s|Downloading\s|Progress\s|Resolving\s|"
    rf"--- \S+:\S+:\S+ .* ---\s*$|"
    rf"BUILD SUCCESS|Total time:|Finished at:)|"
    # Maven WARNING lines about dependency model / pom issues
    rf"^\s*{_TS}\[WARNING\]\s*(?:$|'dependencies|"
    rf"Some problems were|It is highly recommended|"
    rf"\s*from pom\.xml)|"
    # Bare timestamp-only or marker-only lines
    rf"^\s*{_TS}(?:Started by|Running in|Timeout set to expire|"
    rf"Checking out Revision|using credential|Fetching changes|"
    rf"Fetching upstream changes|Commit message:|"
    rf"\> git\s|Cloning repository)\b|"
    # ---- Spring Cloud / Eureka / Netflix infrastructure noise ----
    # Uses .* prefix because .match() anchors to start; class names are mid-line.
    rf".*ConfigServerConfigDataLoader\s*:.*(?:Fetching config|Exception on Url|Connect to .* failed)|"
    rf".*Fetching config from server at\s*:|"
    rf".*RedirectingEurekaHttpClient\s*:.*Request execution error|"
    rf".*RetryableEurekaHttpClient\s*:.*Request execution failed|"
    rf".*DiscoveryClient\S*\s.*(?:was unable to refresh|Cannot execute request|"
    rf"Initial registry fetch|registration failed|DiscoveryClient_)\b|"
    rf".*OptionalValidatorFactoryBean\s*:.*Failed to set up a Bean Validation provider|"
    rf".*TransportException:\s*Cannot execute request on any known server|"
    rf".*jakarta\.ws\.rs\.ProcessingException:\s*java\.net\.ConnectException|"
    rf".*HttpHostConnectException:\s*Connect to http://localhost:\d+\s+failed|"
    rf".*ResourceAccessException:.*Connection refused|"
    rf".*Caused by:\s*java\.net\.ConnectException:\s*Connection refused|"
    # Eureka / Netflix / Jersey / Maven infrastructure stack trace interior
    rf"^\s*{_TS}?\s*at\s+(?:com\.netflix\.discovery\.|org\.glassfish\.jersey\.|"
    rf"jakarta\.ws\.rs\.|org\.apache\.hc\.client5\.|org\.apache\.hc\.core5\.|"
    rf"org\.springframework\.web\.client\.DefaultRestClient|"
    rf"org\.apache\.maven\.lifecycle\.|org\.apache\.maven\.plugin\.|"
    rf"org\.apache\.maven\.DefaultMaven\.|org\.apache\.maven\.cli\.|"
    rf"org\.codehaus\.plexus\.|org\.apache\.maven\.plugin\.surefire\.)\b|"
    rf"^\s*{_TS}?\s*\.\.\.\s+\d+\s+more\s*$|"
    # Spring Boot banner / startup noise
    rf"::\s*Spring Boot\s*::|"
    rf"^\s*{_TS}?\(v\d+\.\d+\.\d+\)\s*$|"
    # Maven Surefire / test harness chatter
    rf"^\s*{_TS}?\[INFO\]\s+(?:Running com\.|Surefire report directory:|"
    rf"Using auto detected provider|T E S T S)\b|"
    rf"Mockito is currently self-attaching|"
    rf"WARNING:\s+A (?:Java agent|terminally deprecated method|restricted method)|"
    rf"WARNING:\s+sun\.misc\.Unsafe|WARNING:\s+Please consider reporting|"
    rf"WARNING:\s+.*will be removed in a future release|"
    rf"WARNING:\s+.*Use --enable-native-access|"
    rf"WARNING:\s+Restricted methods will be blocked"
)


def _cluster_sorted_indices(sorted_indices: list[int], merge_gap: int) -> list[tuple[int, int]]:
    if not sorted_indices:
        return []
    clusters: list[tuple[int, int]] = []
    start = prev = sorted_indices[0]
    for x in sorted_indices[1:]:
        if x - prev <= merge_gap:
            prev = x
        else:
            clusters.append((start, prev))
            start = prev = x
    clusters.append((start, prev))
    return clusters


def _truncate_log_chars(text: str, max_chars: int) -> str:
    """Bound excerpt size while keeping both the start and end of the text.

    Using only ``text[-max_chars:]`` hid pipeline / command context and made excerpts
    appear to begin mid-stack-trace (e.g. ``Connector`` shown as ``nnector``).
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    inner = max(200, max_chars - 96)
    head = max(80, int(inner * 0.55))
    tail = max(80, inner - head)
    for _ in range(40):
        omitted = max(0, len(text) - head - tail)
        notice = f"\n... [{omitted} characters omitted; increase MAX_STAGE_LOG_CHARS] ...\n"
        total = head + len(notice) + tail
        if total <= max_chars:
            spare = max_chars - total
            head += spare // 2
            tail += spare - spare // 2
            omitted = max(0, len(text) - head - tail)
            notice = f"\n... [{omitted} characters omitted; increase MAX_STAGE_LOG_CHARS] ...\n"
            out = text[:head] + notice + text[-tail:]
            if len(out) <= max_chars:
                return out
            return text[: max_chars - 3] + "..."
        head = max(40, head - 30)
        tail = max(40, tail - 30)
    return text[: max_chars - 3] + "..."


class JenkinsClient:
    def __init__(
        self,
        base_url: str,
        user: str,
        api_token: str,
        failed_rss_path: str,
        timeout_seconds: int = 30,
        max_stage_log_chars: int = 50000,
        max_stage_scan_lines: int = 1200,
        per_error_context_before: int = 2,
        per_error_context_after: int = 4,
        error_anchor_merge_gap_lines: int = 3,
        max_error_regions_per_stage: int = 15,
        min_anchor_score_for_snippet: int = 2,
        parallel_stage_overlap_ms: int = 2000,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.failed_rss_path = failed_rss_path
        self.max_stage_log_chars = max_stage_log_chars
        self.max_stage_scan_lines = max_stage_scan_lines
        self.per_error_context_before = per_error_context_before
        self.per_error_context_after = per_error_context_after
        self.error_anchor_merge_gap_lines = error_anchor_merge_gap_lines
        self.max_error_regions_per_stage = max_error_regions_per_stage
        self.min_anchor_score_for_snippet = min_anchor_score_for_snippet
        self.parallel_stage_overlap_ms = parallel_stage_overlap_ms
        self.client = httpx.Client(
            auth=(user, api_token),
            timeout=timeout_seconds,
            follow_redirects=True,
        )

    @staticmethod
    def _job_path(job_full_name: str) -> str:
        return "/".join([f"job/{quote(part, safe='')}" for part in job_full_name.split("/")])

    def list_failed_builds_from_rss(self) -> list[dict]:
        """Parse the Jenkins failed-builds RSS/Atom feed. No time filtering."""
        url = f"{self.base_url}{self.failed_rss_path}"
        response = self.client.get(url)
        response.raise_for_status()

        root = ET.fromstring(response.text)
        items: list[dict] = []

        if root.tag.endswith("feed"):
            atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall("./atom:entry", atom_ns):
                title = (entry.findtext("atom:title", default="", namespaces=atom_ns) or "").strip()
                link_el = entry.find("atom:link", atom_ns)
                link = (link_el.get("href", "") if link_el is not None else "").strip()
                if not title or not link:
                    continue
                parsed = self._parse_title_and_link(title, link)
                if parsed:
                    items.append(parsed)
                else:
                    logger.debug("RSS atom: could not parse title: %s", title[:200])
        else:
            rss_items = root.findall(".//{*}item")
            if not rss_items:
                rss_items = root.findall("./channel/item")
            for item in rss_items:
                title = _rss_child_text(item, "title")
                link = _rss_item_link(item)
                if not title or not link:
                    continue
                parsed = self._parse_title_and_link(title, link)
                if parsed:
                    items.append(parsed)
                else:
                    logger.debug("RSS item: could not parse title: %s", title[:200])

        return items

    @staticmethod
    def _parse_title_and_link(title: str, link: str) -> dict | None:
        # Typical: "my-folder » my-job #123 (Failed)"  (Jenkins classic)
        match = re.search(r"(?P<job>.+)\s+#(?P<num>\d+)\s+\(", title)
        if not match:
            # Some Jenkins themes omit the trailing "(Failed)" clause
            match = re.search(r"(?P<job>.+)\s+#(?P<num>\d+)\s*$", title.strip())
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
        """Resolve failed Pipeline stages via wfapi; fall back to full console if unavailable.

        ``wfapi/describe`` exists only for **Pipeline** (workflow) builds. Freestyle / Maven /
        other job types return **404**, which previously produced empty ``failed_stages`` even
        though the build failed. In that case we attach a single synthetic stage **Build**
        whose excerpt is derived from ``consoleText``.
        """
        job_path = self._job_path(job_full_name)
        url = f"{self.base_url}/{job_path}/{build_number}/wfapi/describe"
        response = self.client.get(url)

        stages: list[dict] = []
        if response.status_code < 400:
            try:
                data = response.json()
                stages = data.get("stages", []) or []
            except Exception:
                logger.warning("%s#%s: wfapi/describe body is not JSON", job_full_name, build_number)
                stages = []
        else:
            logger.info(
                "%s#%s: wfapi/describe HTTP %s (not a Pipeline build or wfapi disabled); "
                "using consoleText fallback",
                job_full_name,
                build_number,
                response.status_code,
            )

        root_failures = self._select_root_failure_stages(stages)
        if root_failures:
            failed: list[FailedStage] = []
            console_text: str | None = None

            for stage in root_failures:
                stage_status = str(stage.get("status", "")).upper()
                stage_id = str(stage.get("id")) if stage.get("id") is not None else None
                stage_name = str(stage.get("name", "unknown-stage"))
                excerpt = self._stage_log_excerpt(job_full_name, build_number, stage_id, stage_name)
                if not excerpt:
                    if console_text is None:
                        console_text = self._build_console_text(job_full_name, build_number)
                    excerpt = self._extract_stage_excerpt_from_console(console_text, stage_name)
                if not excerpt.strip():
                    if console_text is None:
                        console_text = self._build_console_text(job_full_name, build_number)
                    excerpt = self._full_console_excerpt(console_text)
                failed.append(
                    FailedStage(
                        stage_name=stage_name,
                        stage_id=stage_id,
                        status=stage_status,
                        log_excerpt=excerpt,
                    )
                )
            return failed

        # No wfapi failures (freestyle, non-stage pipeline, or only successful stages in describe).
        if stages:
            logger.info(
                "%s#%s: wfapi has %d stage row(s) but none FAILED/ERROR/ABORTED; consoleText fallback",
                job_full_name,
                build_number,
                len(stages),
            )

        console_text = self._build_console_text(job_full_name, build_number)
        if not console_text.strip():
            logger.info("%s#%s: consoleText empty; leaving failed_stages empty", job_full_name, build_number)
            return []

        excerpt = self._full_console_excerpt(console_text)

        return [
            FailedStage(
                stage_name="Build",
                stage_id=None,
                status="FAILURE",
                log_excerpt=excerpt,
            )
        ]

    def _select_root_failure_stages(self, stages: list[dict]) -> list[dict]:
        """Return only the root-cause failed stages — no sequential cascades.

        Builds execution-overlap groups from *all* stages: consecutive stages
        (sorted by start time) that begin while any member of the current
        group is still running are placed in the same group (= parallel
        siblings).  Returns the failed stages from the earliest group that
        contains at least one failure.
        """
        if not stages:
            return []

        error_statuses = {"FAILED", "ERROR", "ABORTED"}
        ordered = sorted(stages, key=lambda s: int(s.get("startTimeMillis") or 0))

        groups: list[list[dict]] = []
        cur_group: list[dict] = [ordered[0]]
        group_end = (
            int(ordered[0].get("startTimeMillis") or 0)
            + int(ordered[0].get("durationMillis") or 0)
        )

        for s in ordered[1:]:
            s_start = int(s.get("startTimeMillis") or 0)
            s_end = s_start + int(s.get("durationMillis") or 0)
            if s_start < group_end:
                cur_group.append(s)
                group_end = max(group_end, s_end)
            else:
                groups.append(cur_group)
                cur_group = [s]
                group_end = s_end
        groups.append(cur_group)

        for group in groups:
            failed_in_group = [
                s for s in group
                if str(s.get("status", "")).upper() in error_statuses
            ]
            if failed_in_group:
                return failed_in_group

        return []

    def _stage_log_excerpt(
        self, job_full_name: str, build_number: int, stage_id: str | None, stage_name: str
    ) -> str:
        if not stage_id:
            return ""
        job_path = self._job_path(job_full_name)
        tag = f"{job_full_name}#{build_number} stage={stage_name}"

        text = self._fetch_node_log_text(job_path, build_number, stage_id)
        if text:
            logger.info("%s: using stage node log (%d chars)", tag, len(text))
            return _truncate_log_chars(text, self.max_stage_log_chars)

        child_text = self._fetch_child_node_logs(job_path, build_number, stage_id)
        if child_text:
            logger.info("%s: using child node logs (%d chars)", tag, len(child_text))
            return _truncate_log_chars(child_text, self.max_stage_log_chars)

        logger.info("%s: no stage-scoped log, falling back to consoleText", tag)
        return ""

    def _fetch_node_log_text(self, job_path: str, build_number: int, node_id: str) -> str:
        """Try wfapi/log for a single node, return cleaned text or empty string."""
        url = f"{self.base_url}/{job_path}/{build_number}/execution/node/{node_id}/wfapi/log"
        try:
            resp = self.client.get(url)
            if resp.status_code < 400:
                data = resp.json()
                raw = str(data.get("text", "")).strip()
                if raw:
                    return _strip_html(raw)
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _fetch_child_node_logs(self, job_path: str, build_number: int, stage_id: str) -> str:
        """Discover stageFlowNodes for a stage and concatenate their logs."""
        desc_url = f"{self.base_url}/{job_path}/{build_number}/execution/node/{stage_id}/wfapi/describe"
        try:
            resp = self.client.get(desc_url)
            if resp.status_code >= 400:
                return ""
            children = resp.json().get("stageFlowNodes", [])
        except Exception:  # noqa: BLE001
            return ""

        parts: list[str] = []
        for child in children:
            cid = str(child.get("id", ""))
            if not cid:
                continue
            text = self._fetch_node_log_text(job_path, build_number, cid)
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _has_real_error_signals(text: str) -> bool:
        if not text:
            return False
        _, failure_signal, _, _ = _log_error_pattern_bundle()
        for line in text.splitlines()[:200]:
            if failure_signal.search(line):
                return True
        return False

    def _build_console_text(self, job_full_name: str, build_number: int) -> str:
        job_path = self._job_path(job_full_name)
        url = f"{self.base_url}/{job_path}/{build_number}/consoleText"
        response = self.client.get(url)
        if response.status_code >= 400:
            return ""
        return response.text or ""

    def _full_console_excerpt(self, console_text: str) -> str:
        """Return full console output (truncated to size limit).

        Used for freestyle / non-workflow jobs (no wfapi stage logs) and whenever
        Pipeline stage-specific excerpts are empty.
        """
        if not (console_text or "").strip():
            return ""
        return _truncate_log_chars(console_text.strip(), self.max_stage_log_chars)

    def _extract_stage_excerpt_from_console(self, console_text: str, stage_name: str) -> str:
        """Extract the stage section from consoleText using Pipeline markers.

        Locates the stage by ``(stage_name)`` markers that Jenkins Pipeline emits and
        returns the surrounding window.  Falls back to the full console text when no
        stage markers are found.
        """
        if not console_text:
            return ""

        lines = console_text.splitlines()
        if not lines:
            return ""

        marker = f"({stage_name})"
        indices = [idx for idx, line in enumerate(lines) if marker in line or stage_name in line]

        if indices:
            center = indices[-1]
            start = max(0, center - 50)
            end = min(len(lines), center + 150)
            window_text = "\n".join(lines[start:end])
            return _truncate_log_chars(window_text, self.max_stage_log_chars)

        return _truncate_log_chars(console_text, self.max_stage_log_chars)

    def _stage_error_snippets(self, text: str) -> str:
        """5-8 lines around each error signal, with overlapping windows merged.

        Produces one compact region per cluster of nearby errors; regions never
        share lines so there are no duplicates in the output.
        """
        lines = text.splitlines()
        if not lines:
            return ""
        n = min(len(lines), self.max_stage_scan_lines)
        bounded = lines[-n:] if len(lines) > n else lines
        before = max(0, self.per_error_context_before)
        after = max(0, self.per_error_context_after)
        merge_gap = max(0, self.error_anchor_merge_gap_lines)
        min_score = self.min_anchor_score_for_snippet

        score_at: dict[int, int] = {}
        for i in range(n):
            sc = _score_line_as_error_anchor(bounded[i])
            if sc is not None and sc >= min_score:
                score_at[i] = max(score_at.get(i, 0), sc)

        if not score_at:
            return self._first_cause_excerpt(text)

        sorted_idx = sorted(score_at)
        clusters = _cluster_sorted_indices(sorted_idx, merge_gap)

        def cluster_peak(lo: int, hi: int) -> int:
            return max(score_at[i] for i in range(lo, hi + 1) if i in score_at)

        if len(clusters) > self.max_error_regions_per_stage:
            ranked = sorted(
                clusters,
                key=lambda ch: (-cluster_peak(ch[0], ch[1]), -ch[1]),
            )
            clusters = sorted(ranked[: self.max_error_regions_per_stage], key=lambda ch: ch[0])

        # Expand each cluster with context then merge overlapping windows.
        windows: list[tuple[int, int]] = []
        for lo, hi in clusters:
            wlo = max(0, lo - before)
            whi = min(n, hi + after + 1)
            if windows and wlo <= windows[-1][1]:
                windows[-1] = (windows[-1][0], max(windows[-1][1], whi))
            else:
                windows.append((wlo, whi))

        parts: list[str] = []
        for _rank, (wlo, whi) in enumerate(windows, start=1):
            chunk = bounded[wlo:whi]
            cleaned = [ln for ln in chunk if ln.strip() and not _LOG_NOISE_LINE.match(ln)]
            if not cleaned:
                continue
            body = "\n".join(cleaned)
            parts.append(body)

        out = "\n\n".join(parts).strip()
        if not out:
            return self._first_cause_excerpt(text)
        return _truncate_log_chars(out, self.max_stage_log_chars)

    def _first_cause_excerpt(self, text: str) -> str:
        """Fallback when no scored error anchors are found.

        Returns a compact tail of the log (often contains the final error
        message or exit status) cleaned of noise lines.
        """
        lines = text.splitlines()
        if not lines:
            return ""
        tail = lines[-min(30, len(lines)):]
        cleaned = [ln for ln in tail if ln.strip() and not _LOG_NOISE_LINE.match(ln)]
        if not cleaned:
            cleaned = [ln for ln in tail if ln.strip()]
        result = "\n".join(cleaned[-8:])
        return _truncate_log_chars(result, self.max_stage_log_chars)

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
            timestamp=datetime.now(UTC),
            correlation_id=str(uuid4()),
            failed_stages=failed_stages,
        )
