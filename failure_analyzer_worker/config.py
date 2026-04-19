import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_DIR = Path(__file__).resolve().parent


def _prefer_worker_dotenv_for_elasticsearch() -> None:
    """Copy ES keys from this package's ``.env`` into ``os.environ`` so they override stale shell vars."""
    path = _PACKAGE_DIR / ".env"
    if not path.is_file():
        return
    vals = dotenv_values(path)
    for key in ("ELASTICSEARCH_URL", "ELASTICSEARCH_INDEX", "ELASTICSEARCH_API_KEY"):
        if key not in vals:
            continue
        v = vals[key]
        if v is None:
            continue
        os.environ[key] = v


_prefer_worker_dotenv_for_elasticsearch()


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PACKAGE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Elasticsearch
    elasticsearch_url: str = Field(
        default="http://localhost:9200", alias="ELASTICSEARCH_URL"
    )
    elasticsearch_index: str = Field(
        default="failure_solutions", alias="ELASTICSEARCH_INDEX"
    )
    elasticsearch_context_index: str = Field(
        default="failure_context", alias="ELASTICSEARCH_CONTEXT_INDEX"
    )
    elasticsearch_api_key: str = Field(default="", alias="ELASTICSEARCH_API_KEY")
    # Document retention (delete_by_query on ``created_at``). Two indices only: solutions + context.
    elasticsearch_retention_solutions_days: int = Field(
        default=1095, alias="ELASTICSEARCH_RETENTION_SOLUTIONS_DAYS"
    )  # ~36 months
    elasticsearch_retention_context_days: int = Field(
        default=548, alias="ELASTICSEARCH_RETENTION_CONTEXT_DAYS"
    )  # ~18 months
    similarity_threshold: float = Field(default=0.75, alias="SIMILARITY_THRESHOLD")
    similarity_top_k: int = Field(default=3, alias="SIMILARITY_TOP_K")

    # Embedding model (sentence-transformers)
    embedding_model: str = Field(
        default="all-MiniLM-L6-v2", alias="EMBEDDING_MODEL"
    )

    # LLM (Groq)
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    llm_model: str = Field(default="llama-3.3-70b-versatile", alias="LLM_MODEL")
    llm_temperature: float = Field(default=0.1, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=2048, alias="LLM_MAX_TOKENS")
    # Set to "0"/"false" only when running behind a corporate MITM proxy
    # with a self-signed root CA. Default verifies TLS.
    llm_tls_verify: str = Field(default="1", alias="LLM_TLS_VERIFY")

    # Worker HTTP
    worker_host: str = Field(default="127.0.0.1", alias="WORKER_HOST")
    worker_port: int = Field(default=8090, alias="WORKER_PORT")
    worker_api_key: str = Field(default="replace-me", alias="WORKER_API_KEY")

    # Optional: POST each ingest result to the web API so the dashboard can list sessions (set in .env for local dev).
    web_ui_api_url: str = Field(default="", alias="WEB_UI_API_URL")
    web_ui_public_url: str = Field(
        default="http://127.0.0.1:3000", alias="WEB_UI_PUBLIC_URL"
    )

    # Log filtering pipeline (normalize → partition → dedupe → anchors → noise)
    log_filter_legacy: bool = Field(default=False, alias="LOG_FILTER_LEGACY")
    log_max_filtered_chars: int = Field(default=16000, alias="LOG_MAX_FILTERED_CHARS")
    log_anchor_before_lines: int = Field(default=14, alias="LOG_ANCHOR_BEFORE_LINES")
    log_anchor_after_lines: int = Field(default=18, alias="LOG_ANCHOR_AFTER_LINES")
    log_anchor_context_before: int = Field(
        default=2,
        alias="LOG_ANCHOR_CONTEXT_BEFORE",
    )
    log_dedupe_min_consecutive: int = Field(
        default=4,
        alias="LOG_DEDUPE_MIN_CONSECUTIVE",
    )
    log_partition_by_service: bool = Field(
        default=True, alias="LOG_PARTITION_BY_SERVICE"
    )
    # Prefer earliest anchors in file order (often root cause before cascade).
    log_anchor_chronological: bool = Field(default=True, alias="LOG_ANCHOR_CHRONOLOGICAL")
    log_anchor_max_points: int = Field(default=12, alias="LOG_ANCHOR_MAX_POINTS")
    log_first_failure_min_score: int = Field(
        default=4, alias="LOG_FIRST_FAILURE_MIN_SCORE"
    )
    log_shrink_later_buckets: bool = Field(default=True, alias="LOG_SHRINK_LATER_BUCKETS")
    log_later_bucket_after_lines: int = Field(
        default=8, alias="LOG_LATER_BUCKET_AFTER_LINES"
    )
    log_later_bucket_before_lines: int = Field(
        default=8, alias="LOG_LATER_BUCKET_BEFORE_LINES"
    )
    log_later_bucket_max_chars: int = Field(
        default=4200, alias="LOG_LATER_BUCKET_MAX_CHARS"
    )
    log_dedupe_same_root_bucket: bool = Field(
        default=True, alias="LOG_DEDUPE_SAME_ROOT_BUCKET"
    )
    # Collapse runs of spaces/tabs per line so ``len(filtered_logs)`` counts fewer chars.
    log_compress_whitespace: bool = Field(
        default=True, alias="LOG_COMPRESS_WHITESPACE"
    )
    # Max chars for the log body in ``LogProcessor`` output (metadata is outside this cap).
    log_body_max_chars: int = Field(default=5000, alias="LOG_BODY_MAX_CHARS")


settings = WorkerSettings()
