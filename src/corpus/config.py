"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Accept the libpq-style URLs hosting platforms hand out (Render, Neon, Supabase...).

    * ``postgres://`` / ``postgresql://`` -> ``postgresql+asyncpg://``
    * ``sslmode=require`` -> ``ssl=require`` (asyncpg's spelling)
    * ``channel_binding`` is dropped (not supported by asyncpg; TLS still applies)
    """
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+asyncpg://" + url[len(prefix) :]
            break
    parts = urlsplit(url)
    if not parts.query:
        return url
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        query.append(("ssl" if key == "sslmode" else key, value))
    return urlunsplit(parts._replace(query=urlencode(query)))


EmbeddingProvider = Literal["hash", "fastembed", "voyage"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    # --- Single-instance deployment (free hosting tiers, demos) -------------
    auto_migrate: bool = Field(
        default=False, description="Apply Alembic migrations when the API starts."
    )
    embedded_worker: bool = Field(
        default=False, description="Run the ingestion worker inside the API process."
    )
    demo_mode: bool = Field(
        default=False,
        description="Seed the sample documents at startup and serve /demo pre-configured.",
    )
    public_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("public_url", "render_external_url"),
        description="Public base URL (Render provides RENDER_EXTERNAL_URL automatically).",
    )

    # --- API ---------------------------------------------------------------
    admin_api_key: SecretStr | None = Field(
        default=None,
        description="Enables the /admin routes (tenant and API-key management) via X-Admin-Key.",
    )
    default_rate_limit_per_minute: int = Field(default=120, ge=1)
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    max_upload_mb: int = 25
    log_level: str = "INFO"

    # --- Observability -------------------------------------------------------
    log_format: Literal["json", "text"] = "json"
    metrics_enabled: bool = True
    worker_metrics_port: int = Field(default=9100, ge=1, le=65535)
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None,
        description="OTLP/HTTP collector base URL (e.g. http://jaeger:4318). Unset = no tracing.",
    )
    otel_service_name: str | None = None

    # --- Database ----------------------------------------------------------
    database_url: str = "postgresql+asyncpg://corpus:corpus@localhost:5432/corpus"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout_seconds: float = Field(default=5.0, gt=0)

    # --- Job queue / workers -------------------------------------------------
    worker_concurrency: int = Field(default=4, ge=1)
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
    job_lease_seconds: int = Field(default=300, ge=10)
    job_max_attempts: int = Field(default=5, ge=1)
    job_backoff_base_seconds: float = Field(default=5.0, ge=0)
    job_backoff_max_seconds: float = Field(default=600.0, ge=0)

    # --- Embeddings --------------------------------------------------------
    embedding_provider: EmbeddingProvider = "fastembed"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    embedding_batch_size: int = 64
    voyage_api_key: SecretStr | None = None

    # --- Parsing -----------------------------------------------------------
    ocr: Literal["auto", "tesseract", "off"] = "auto"
    ocr_languages: str = "eng+fra+deu"

    # --- Chunking ----------------------------------------------------------
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 60

    # --- Retrieval ---------------------------------------------------------
    retrieval_candidates: int = 30
    retrieval_top_k: int = 6
    rrf_k: int = 60
    reranker: Literal["none", "cross-encoder"] = "none"
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    rerank_candidates: int = Field(default=20, ge=1, le=100)
    query_rewriting: bool = Field(
        default=True,
        description="Rewrite follow-up questions into standalone search queries (needs an LLM).",
    )

    # --- Generation (LLM) ----------------------------------------------------
    # Any OpenAI-compatible chat-completions endpoint: a hosted provider or a self-hosted
    # server (vLLM, Ollama...). The provider is a configuration choice, not a code dependency.
    llm_provider: Literal["openai-compatible", "extractive"] = Field(
        default="openai-compatible",
        description="'extractive' answers offline by quoting the best source: demos, load tests.",
    )
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_max_tokens: int = Field(default=1500, ge=1)
    llm_temperature: float = Field(default=0.2, ge=0, le=2)
    extractive_token_delay_ms: float = Field(default=0.0, ge=0)

    @property
    def llm_configured(self) -> bool:
        return self.llm_api_key is not None and bool(self.llm_model)

    @field_validator(
        "admin_api_key",
        "llm_api_key",
        "voyage_api_key",
        "otel_exporter_otlp_endpoint",
        "llm_model",
        mode="before",
    )
    @classmethod
    def _blank_secret_is_unset(cls, value: object) -> object:
        # `LLM_API_KEY=` in a .env file (or an unset CI secret) must mean "no key",
        # not "a key that is the empty string".
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("database_url")
    @classmethod
    def _asyncpg_driver(cls, url: str) -> str:
        return normalize_database_url(url)

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            raise ValueError("chunk_overlap_tokens must be smaller than chunk_max_tokens")
        if self.retrieval_top_k > self.retrieval_candidates:
            raise ValueError("retrieval_top_k must be <= retrieval_candidates")
        if self.embedding_provider == "voyage" and self.voyage_api_key is None:
            raise ValueError("VOYAGE_API_KEY is required when EMBEDDING_PROVIDER=voyage")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
