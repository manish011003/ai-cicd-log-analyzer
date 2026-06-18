"""Worker settings.

Organized around **provider slots** so swapping LLMs, embedding models, or
vector stores is purely an env change:

  LLM_PROVIDER        → groq | openai | anthropic | ollama
  EMBEDDING_PROVIDER  → sentence_transformers | openai
  VECTOR_STORE_PROVIDER → elasticsearch

Each slot reads the same generic keys (``LLM_API_KEY`` / ``LLM_API_BASE`` /
``LLM_MODEL``, etc.) so you don't have to rename variables when you change
vendors. ``GROQ_API_KEY`` is kept as a legacy fallback for Groq only.
"""

from __future__ import annotations

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

    # ── Vector store ──────────────────────────────────────────────────────────
    vector_store_provider: str = Field(
        default="elasticsearch", alias="VECTOR_STORE_PROVIDER",
    )
    similarity_threshold: float = Field(default=0.75, alias="SIMILARITY_THRESHOLD")
    similarity_top_k: int = Field(default=3, alias="SIMILARITY_TOP_K")
    similarity_num_candidates: int = Field(
        default=50, alias="SIMILARITY_NUM_CANDIDATES",
    )

    # ── Elasticsearch (only read when VECTOR_STORE_PROVIDER=elasticsearch) ─────
    elasticsearch_url: str = Field(
        default="http://localhost:9200", alias="ELASTICSEARCH_URL",
    )
    elasticsearch_index: str = Field(
        default="failure_solutions", alias="ELASTICSEARCH_INDEX",
    )
    elasticsearch_api_key: str = Field(default="", alias="ELASTICSEARCH_API_KEY")
    elasticsearch_request_timeout_seconds: int = Field(
        default=30, alias="ELASTICSEARCH_REQUEST_TIMEOUT_SECONDS",
    )
    # Document retention (delete_by_query on ``created_at``).
    elasticsearch_retention_solutions_days: int = Field(
        default=1095, alias="ELASTICSEARCH_RETENTION_SOLUTIONS_DAYS",
    )  # ~36 months

    # ── Embeddings ────────────────────────────────────────────────────────────
    embedding_provider: str = Field(
        default="sentence_transformers", alias="EMBEDDING_PROVIDER",
    )
    embedding_model: str = Field(default="all-MiniLM-L6-v2", alias="EMBEDDING_MODEL")
    embedding_dimensions: int = Field(default=0, alias="EMBEDDING_DIMENSIONS")
    embedding_api_key: str = Field(default="", alias="EMBEDDING_API_KEY")
    embedding_api_base: str = Field(default="", alias="EMBEDDING_API_BASE")
    # Set to "0" only behind a corporate MITM proxy with a self-signed root CA.
    # HuggingFace LFS-served model blobs go through a CDN whose TLS chain the
    # container won't trust; without this the first model download hangs and
    # the worker never finishes startup.
    embedding_tls_verify: str = Field(default="1", alias="EMBEDDING_TLS_VERIFY")

    # ── LLM ───────────────────────────────────────────────────────────────────
    llm_provider: str = Field(default="groq", alias="LLM_PROVIDER")
    llm_model: str = Field(default="llama-3.3-70b-versatile", alias="LLM_MODEL")
    llm_temperature: float = Field(default=0.1, alias="LLM_TEMPERATURE")
    llm_max_tokens: int = Field(default=2048, alias="LLM_MAX_TOKENS")
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_api_base: str = Field(default="", alias="LLM_API_BASE")
    llm_tls_verify: str = Field(default="1", alias="LLM_TLS_VERIFY")

    # Legacy fallback for Groq users on older .env files.
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")

    # ── Prompt overrides ──────────────────────────────────────────────────────
    prompts_dir: str = Field(default="", alias="PROMPTS_DIR")

    # ── Worker HTTP ──────────────────────────────────────────────────────────
    worker_host: str = Field(default="127.0.0.1", alias="WORKER_HOST")
    worker_port: int = Field(default=8090, alias="WORKER_PORT")
    worker_api_key: str = Field(default="replace-me", alias="WORKER_API_KEY")

    # ── Web UI hook ──────────────────────────────────────────────────────────
    web_ui_api_url: str = Field(default="", alias="WEB_UI_API_URL")
    web_ui_public_url: str = Field(
        default="http://127.0.0.1:3000", alias="WEB_UI_PUBLIC_URL",
    )
    web_ui_session_timeout_seconds: float = Field(
        default=20.0, alias="WEB_UI_SESSION_TIMEOUT_SECONDS",
    )

    # ── Log body cap ──────────────────────────────────────────────────────────
    # The structural filter (`failure_analyzer_worker.filtering`) budgets in
    # *tokens* (Pass 4 round-robin selection), and only falls back to the
    # char cap as a hard ceiling when something pathological slips through.
    # Approximation: ~4 chars per token works for all common BPE tokenizers.
    log_body_max_tokens: int = Field(default=1500, alias="LOG_BODY_MAX_TOKENS")
    log_body_max_chars: int = Field(default=8000, alias="LOG_BODY_MAX_CHARS")

    # ── Filter detectors (plug-and-play) ─────────────────────────────────────
    # "auto"   → load every bundled detector; each detector's own activates_on
    #            decides whether it runs per log.
    # "none"   → universal core only; no detectors.
    # "a,b,c"  → explicit allowlist by detector name (see filtering/detectors/).
    filter_detectors: str = Field(default="auto", alias="FILTER_DETECTORS")
    filter_max_active_detectors: int = Field(
        default=5, alias="FILTER_MAX_ACTIVE_DETECTORS",
    )

    # Cap the raw log excerpt persisted alongside each session. Keep this
    # bounded so Postgres rows don't explode on pathological builds; the
    # listener's own MAX_STAGE_LOG_CHARS already limits upstream.
    raw_log_max_chars: int = Field(default=200_000, alias="RAW_LOG_MAX_CHARS")

    # Cap the log excerpt forwarded to the LLM in the *chat* path. Default is
    # tight (fast, cheap); when the user toggles "include raw log excerpt" in
    # the UI we raise it to ``chat_log_excerpt_max_chars_full``.
    chat_log_excerpt_max_chars: int = Field(
        default=12_000, alias="CHAT_LOG_EXCERPT_MAX_CHARS",
    )
    chat_log_excerpt_max_chars_full: int = Field(
        default=60_000, alias="CHAT_LOG_EXCERPT_MAX_CHARS_FULL",
    )


settings = WorkerSettings()
