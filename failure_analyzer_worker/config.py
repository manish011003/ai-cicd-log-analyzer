from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PACKAGE_DIR = Path(__file__).resolve().parent


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_PACKAGE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Elasticsearch
    elasticsearch_url: str = Field(
        default="http://localhost:9201", alias="ELASTICSEARCH_URL"
    )
    elasticsearch_index: str = Field(
        default="failure_solutions", alias="ELASTICSEARCH_INDEX"
    )
    elasticsearch_api_key: str = Field(default="", alias="ELASTICSEARCH_API_KEY")
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

    # Worker HTTP
    worker_host: str = Field(default="127.0.0.1", alias="WORKER_HOST")
    worker_port: int = Field(default=8090, alias="WORKER_PORT")
    worker_api_key: str = Field(default="replace-me", alias="WORKER_API_KEY")


settings = WorkerSettings()
