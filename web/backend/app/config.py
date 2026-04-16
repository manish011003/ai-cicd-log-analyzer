from pathlib import Path

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


settings = Settings()
