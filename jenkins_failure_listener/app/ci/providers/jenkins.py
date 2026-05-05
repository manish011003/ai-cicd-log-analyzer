"""Jenkins adapter for :class:`CISource`.

Thin wrapper that composes the existing :class:`app.jenkins_client.JenkinsClient`
(which still owns all the Jenkins-specific RSS / wfapi / console-log parsing).
Only the neutral protocol methods are exposed here — callers of
:class:`CISource` must not depend on Jenkins-specific internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.jenkins_client import JenkinsClient
from app.models import FailureEvent

from ..base import FailedBuildRef

if TYPE_CHECKING:
    from app.config import Settings


class JenkinsCISource:
    """CISource that talks to a Jenkins controller via its RSS + REST APIs."""

    def __init__(self, settings: "Settings") -> None:
        self._client = JenkinsClient(
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
            parallel_block_edge_scan_ids=settings.parallel_block_edge_scan_ids,
        )

    def list_failed_builds(self) -> list[FailedBuildRef]:
        return [
            FailedBuildRef(
                job_full_name=item["job_full_name"],
                build_number=int(item["build_number"]),
                build_url=str(item["build_url"]),
            )
            for item in self._client.list_failed_builds_from_rss()
        ]

    def build_failure_event(
        self,
        job_full_name: str,
        build_number: int,
        build_url: str,
    ) -> FailureEvent:
        return self._client.build_failure_event(job_full_name, build_number, build_url)

    # Exposed only for legacy callers (tests / diagnostics) that still want
    # access to the raw Jenkins client. New code should not rely on this.
    @property
    def jenkins_client(self) -> JenkinsClient:
        return self._client
