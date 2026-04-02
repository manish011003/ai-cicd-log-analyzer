from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    jenkins_base_url: str = Field(alias="JENKINS_BASE_URL")
    jenkins_user: str = Field(alias="JENKINS_USER")
    jenkins_api_token: str = Field(alias="JENKINS_API_TOKEN")
    jenkins_failed_rss_path: str = Field(default="/rssFailed", alias="JENKINS_FAILED_RSS_PATH")

    poll_interval_seconds: int = Field(default=20, alias="POLL_INTERVAL_SECONDS")
    initial_lookback_minutes: int = Field(default=60, alias="INITIAL_LOOKBACK_MINUTES")
    steady_lookback_minutes: int = Field(default=5, alias="STEADY_LOOKBACK_MINUTES")
    database_url: str = Field(alias="DATABASE_URL")
    state_retention_days: int = Field(default=30, alias="STATE_RETENTION_DAYS")

    worker_ingest_url: str = Field(alias="WORKER_INGEST_URL")
    worker_ingest_api_key: str = Field(alias="WORKER_INGEST_API_KEY")

    max_stage_log_chars: int = Field(default=30000, alias="MAX_STAGE_LOG_CHARS")
    request_timeout_seconds: int = Field(default=30, alias="REQUEST_TIMEOUT_SECONDS")


settings = Settings()
