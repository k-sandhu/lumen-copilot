"""Application configuration — the single source of runtime config.

This module is the **only** place in the backend that reads the environment
(ADR-0004 boundary table: "Config & secrets -> core/config.py"). Everything
else receives a ``Settings`` instance via dependency injection. No
``os.environ`` reads, no hardcoded config, no secrets in code (AGENTS.md §6 /
backend/AGENTS.md).

Settings fail fast: a missing required value raises at construction, so the
process refuses to boot misconfigured rather than failing deep in a request.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed runtime configuration, sourced from the environment.

    Field names map to the env vars defined in the repo-root ``.env.example``
    and consumed by ``docker-compose.yml``. Defaults exist only for values that
    are genuinely optional for the skeleton to boot (e.g. a blank LLM key);
    infrastructure URLs are required so misconfiguration fails fast.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Service identity (surfaced by /health) ---
    service_name: str = "lumen-copilot-backend"
    version: str = "0.0.1"

    # --- Environment / observability ---
    environment: str = Field(default="local", alias="ENVIRONMENT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    # --- Identity & auth (CC-3 / spec 0004 §2.3) ---
    # Symmetric signing secret for the access JWT. A dev default is provided so
    # the skeleton boots; production MUST override it (a deploy with the default
    # in a non-local environment fails fast — see the validator below). The
    # refresh token is opaque/random, not signed, so it has no separate secret.
    jwt_secret: str = Field(default="dev-only-insecure-jwt-secret-change-me", alias="JWT_SECRET")
    # HS256 keeps key management to one symmetric secret for the app-managed MVP;
    # the OIDC end-state (Keycloak, spec 0004 §2.3) swaps this for asymmetric
    # verification inside auth/ without touching callers.
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_issuer: str = Field(default="lumen-copilot", alias="JWT_ISSUER")
    # Short-lived access token (spec 0004 §2.3: <= 15 min).
    access_token_ttl_seconds: int = Field(default=900, alias="ACCESS_TOKEN_TTL_SECONDS")
    # Rotating refresh token lifetime (default 14 days). Each use rotates it.
    refresh_token_ttl_seconds: int = Field(
        default=14 * 24 * 3600, alias="REFRESH_TOKEN_TTL_SECONDS"
    )

    @field_validator("access_token_ttl_seconds")
    @classmethod
    def _cap_access_ttl(cls, value: int) -> int:
        """Enforce the spec 0004 §2.3 ceiling: access tokens are <= 15 minutes."""
        if value <= 0 or value > 900:
            raise ValueError("ACCESS_TOKEN_TTL_SECONDS must be in (0, 900] (spec 0004 §2.3)")
        return value

    # --- Datastores / infra (required: misconfig should fail fast) ---
    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")
    celery_broker_url: str = Field(alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(alias="CELERY_RESULT_BACKEND")

    # --- Object storage (S3 / MinIO) ---
    s3_endpoint_url: str = Field(alias="S3_ENDPOINT_URL")
    s3_access_key: str = Field(alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(alias="S3_SECRET_KEY")
    s3_bucket: str = Field(alias="S3_BUCKET")

    # --- Upload sandbox (CC-12 / issue #22) ---
    # TTL for presigned PUT/GET URLs. Short by design: a leaked URL expires fast,
    # and the actual transfer still goes directly to/from storage, not through
    # the API process (AC-3).
    s3_presign_ttl_seconds: int = Field(default=900, alias="S3_PRESIGN_TTL_SECONDS")
    # Hard upper bound on a single uploaded object (bytes). Default 50 MiB.
    # Validated before storing so an over-limit upload is a typed 4xx, never a
    # silent drop or a 500 (AC-4 / AC-6).
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")
    # Allowlisted upload content-types (AC-4). The declared type is checked
    # against this set before storing. NOTE: a client-declared content-type is
    # not a security guarantee — sniffing/parsing-sandbox hardening is CC-5/OD-4,
    # fenced OUT of #22. Comma-separated override via UPLOAD_ALLOWED_CONTENT_TYPES.
    upload_allowed_content_types: frozenset[str] = Field(
        default=frozenset(
            {
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
                "text/plain",
                "text/markdown",
            }
        ),
        alias="UPLOAD_ALLOWED_CONTENT_TYPES",
    )

    @field_validator("upload_allowed_content_types", mode="before")
    @classmethod
    def _split_content_types(cls, value: object) -> object:
        """Accept a comma-separated env string as the content-type allowlist."""
        if isinstance(value, str):
            return frozenset(item.strip() for item in value.split(",") if item.strip())
        return value

    # --- LLM gateway (LiteLLM -> OpenRouter first; key may be blank) ---
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    llm_model: str = Field(default="openrouter/openai/gpt-4o-mini", alias="LLM_MODEL")
    # Embedding model id (issue #32). OpenRouter serves embeddings on an
    # OpenAI-compatible endpoint; LiteLLM's native ``openrouter/`` route for
    # embeddings is unreliable (BerriAI/litellm#17773), so embeddings go through
    # LiteLLM's OpenAI-compatible client pointed at ``llm_embedding_api_base``
    # with the OpenRouter key — chat keeps the native ``openrouter/`` route.
    # Hence the ``openai/<author>/<model>`` form: LiteLLM strips ``openai/`` and
    # sends ``baai/bge-m3`` to the configured base.
    llm_embedding_model: str = Field(
        default="openai/baai/bge-m3",
        alias="LLM_EMBEDDING_MODEL",
    )
    # Base URL embeddings are sent to (OpenRouter's OpenAI-compatible endpoint).
    # Blank disables the override — use only with a model LiteLLM routes natively.
    llm_embedding_api_base: str = Field(
        default="https://openrouter.ai/api/v1",
        alias="LLM_EMBEDDING_API_BASE",
    )
    # Output dimension of ``llm_embedding_model`` (bge-m3 = 1024). Pins the
    # pgvector column width for the ingestion migration; change with the model.
    llm_embedding_dimensions: int = Field(default=1024, alias="LLM_EMBEDDING_DIMENSIONS")
    # Per-request wall-clock budget handed to LiteLLM so a stalled provider
    # surfaces as a typed timeout rather than hanging the caller (AC-4, AC-7).
    llm_timeout_seconds: float = Field(default=60.0, alias="LLM_TIMEOUT_SECONDS")

    @property
    def llm_enabled(self) -> bool:
        """True when an LLM provider key is configured.

        The gateway no-ops gracefully when this is False so the skeleton boots
        and runs without any model provider (per the compose contract — the key
        may be left blank in ``.env``).
        """
        return bool(self.openrouter_api_key.strip())

    _DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me"

    @model_validator(mode="after")
    def _reject_dev_jwt_secret_in_prod(self) -> Settings:
        """Fail fast if the insecure dev JWT secret leaks into a deployed env.

        The dev default lets the skeleton boot locally; any non-``local``
        environment that still carries it is a misconfiguration that must refuse
        to start rather than mint forgeable tokens (spec 0004 §2.3).
        """
        if self.environment != "local" and self.jwt_secret == self._DEV_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET must be overridden outside the local environment (spec 0004 §2.3)"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the environment is parsed once. Tests can clear the cache via
    ``get_settings.cache_clear()`` to inject a different environment.
    """
    return Settings()  # values come from env/.env (pydantic-settings)
