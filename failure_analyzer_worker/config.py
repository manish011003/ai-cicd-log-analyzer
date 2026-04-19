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

    # Log body cap (metadata header is outside this cap).
    # The listener already crops the log to the relevant region; the worker
    # only cleans timestamps/noise/dupes and trims to this cap if needed.
    log_body_max_chars: int = Field(default=8000, alias="LOG_BODY_MAX_CHARS")


settings = WorkerSettings()
