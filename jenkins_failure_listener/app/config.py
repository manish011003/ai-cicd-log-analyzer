from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve `.env` next to this package (`jenkins_failure_listener/.env`), not the process cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # CI source selection — swap provider by changing this one env var once
    # additional adapters land under ``app/ci/providers/``.
    ci_provider: str = Field(default="jenkins", alias="CI_PROVIDER")

    jenkins_base_url: str = Field(alias="JENKINS_BASE_URL")
    jenkins_user: str = Field(alias="JENKINS_USER")
    jenkins_api_token: str = Field(alias="JENKINS_API_TOKEN")
    jenkins_failed_rss_path: str = Field(default="/rssFailed", alias="JENKINS_FAILED_RSS_PATH")

    poll_interval_seconds: int = Field(default=20, alias="POLL_INTERVAL_SECONDS")
    database_url: str = Field(alias="DATABASE_URL")
    state_retention_days: int = Field(default=30, alias="STATE_RETENTION_DAYS")

    worker_ingest_url: str = Field(alias="WORKER_INGEST_URL")
    worker_ingest_api_key: str = Field(alias="WORKER_INGEST_API_KEY")
    worker_send_batch: bool = Field(default=True, alias="WORKER_SEND_BATCH")

    max_stage_log_chars: int = Field(default=50000, alias="MAX_STAGE_LOG_CHARS")
    max_stage_scan_lines: int = Field(default=1200, alias="MAX_STAGE_SCAN_LINES")
    per_error_context_before: int = Field(default=2, alias="PER_ERROR_CONTEXT_BEFORE")
    per_error_context_after: int = Field(default=4, alias="PER_ERROR_CONTEXT_AFTER")
    error_anchor_merge_gap_lines: int = Field(default=3, alias="ERROR_ANCHOR_MERGE_GAP_LINES")
    max_error_regions_per_stage: int = Field(default=15, alias="MAX_ERROR_REGIONS_PER_STAGE")
    min_anchor_score_for_snippet: int = Field(default=2, alias="MIN_ANCHOR_SCORE_FOR_SNIPPET")
    parallel_stage_overlap_ms: int = Field(default=2000, alias="PARALLEL_STAGE_OVERLAP_MS")
    # How many wfapi node ids we probe at each *edge* of the gap between
    # consecutive top-level stages when detecting Jenkins's own
    # ``Execute in parallel : Start`` / ``: End`` markers. Forward edge
    # scans run from the previous stage; backward scans run only when an
    # open parallel block is on the stack and look immediately before the
    # next stage. Real Jenkins emits both markers within a handful of ids
    # of the surrounding stages, so 8 covers the common case while
    # bounding HTTP cost. Each probe = one wfapi call.
    parallel_block_edge_scan_ids: int = Field(
        default=8, alias="PARALLEL_BLOCK_EDGE_SCAN_IDS",
    )
    request_timeout_seconds: int = Field(default=30, alias="REQUEST_TIMEOUT_SECONDS")


settings = Settings()
