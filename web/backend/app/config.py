from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    worker_base_url: str = "http://127.0.0.1:8090"
    worker_api_key: str = "replace-me"
    # Must match Jenkins listener HTTP mode: ``uvicorn app.main:app --port 8088`` (see jenkins_failure_listener/ingestion.md).
    listener_base_url: str = "http://127.0.0.1:8088"
    cors_origins: str = "*"

    # ── Retention (Postgres) ─────────────────────────────────────────────────
    # Hard TTL for analysis sessions. Rows older than this are deleted (with
    # their chat messages via ON DELETE CASCADE). Defaults roughly map to:
    #   sessions ~6 months, chat messages ~3 months.
    session_retention_days: int = Field(default=180, alias="SESSION_RETENTION_DAYS")
    message_retention_days: int = Field(default=90, alias="MESSAGE_RETENTION_DAYS")
    # How often the background janitor runs. 0 disables the scheduler entirely
    # (useful for unit tests). Production default is once per 24h.
    retention_run_interval_hours: int = Field(
        default=24, alias="RETENTION_RUN_INTERVAL_HOURS",
    )


settings = Settings()
