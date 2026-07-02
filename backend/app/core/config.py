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

import json
from functools import lru_cache

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__ as _APP_VERSION
from app.domain.models import ModelTier


class ChatModelSetting(BaseModel):
    """One entry in the curated chat-model registry (issue #47, AC-2).

    A typed config row, not a router constant: the picker registry is config so
    adding/removing a model is an env change. Field-for-field the domain
    :class:`~app.domain.models.ChatModel`; ``core`` keeps no dependency on
    ``services``, so the service maps this to the domain type. ``tier`` is the
    contract enum (frontier/fast/oss) and is validated on construction.
    """

    model_config = {"extra": "forbid", "frozen": True}

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    tier: ModelTier
    is_default: bool = False
    description: str | None = None


# Seed registry (issue #47 AC-2): curated, config-driven, exactly one default.
# Ids are provider-qualified as the chat runtime expects (and as listed on
# OpenRouter). Override wholesale via the CHAT_MODEL_REGISTRY env var (JSON).
_DEFAULT_CHAT_MODEL_REGISTRY: tuple[ChatModelSetting, ...] = (
    # ``id`` is the LiteLLM routing id: the OpenRouter gateway routes via the
    # ``openrouter/`` prefix (matching the LLM_MODEL default). Without it LiteLLM
    # would treat e.g. ``anthropic/...`` as a direct Anthropic call (no key →
    # AuthenticationError). ``label`` is the clean display name for the picker.
    ChatModelSetting(
        id="openrouter/anthropic/claude-opus-4.8",
        label="Claude Opus 4.8",
        provider="anthropic",
        tier=ModelTier.FRONTIER,
        is_default=True,
    ),
    ChatModelSetting(
        id="openrouter/openai/gpt-5.5",
        label="GPT-5.5",
        provider="openai",
        tier=ModelTier.FRONTIER,
    ),
    ChatModelSetting(
        id="openrouter/google/gemini-3.5-flash",
        label="Gemini 3.5 Flash",
        provider="google",
        tier=ModelTier.FAST,
    ),
    ChatModelSetting(
        id="openrouter/anthropic/claude-haiku-4.5",
        label="Claude Haiku 4.5",
        provider="anthropic",
        tier=ModelTier.FAST,
    ),
    ChatModelSetting(
        id="openrouter/deepseek/deepseek-v3.2",
        label="DeepSeek V3.2",
        provider="deepseek",
        tier=ModelTier.OSS,
    ),
    ChatModelSetting(
        id="openrouter/qwen/qwen3.7-max",
        label="Qwen3.7 Max",
        provider="qwen",
        tier=ModelTier.OSS,
    ),
)


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
    # Sourced once from the package version (app.__version__, mirroring
    # pyproject.toml) so the value served by /health and the OpenAPI title cannot
    # drift from the package when someone bumps the release — not a re-typed
    # literal. Override per-deploy via the VERSION env var if needed.
    version: str = _APP_VERSION

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

    # When true (default), GET /documents/{id}/content responds 302 to a
    # short-TTL presigned GET URL (CC-12, the contract's primary path) so the
    # bytes transfer directly from storage, not through the API process. When
    # false, the API streams the bytes inline (application/octet-stream) — useful
    # where a redirect is undesirable (e.g. same-origin embedding). Both are
    # contract-valid (the 200 and 302 responses are both defined).
    document_content_redirect: bool = Field(default=True, alias="DOCUMENT_CONTENT_REDIRECT")

    # --- OpenSearch — the single retrieval store (ADR-0010) ---
    # BM25 lexical + kNN vectors in one engine behind ``app/search/``. Inside
    # compose the URL is the service name (``.env``); the default targets the
    # compose-mapped HOST port so dev/tests outside the containers reach the same
    # engine. An unreachable engine fails retrieval CLOSED (503), never an
    # unfiltered fallback.
    opensearch_url: str = Field(default="http://localhost:47186", alias="OPENSEARCH_URL")
    opensearch_index: str = Field(default="lumen-chunks", alias="OPENSEARCH_INDEX")
    # 30s default (#258): bulk writes carry ~20KB-per-chunk embedding payloads
    # and kNN graph insertion is not instant; 10s proved too tight for real
    # batches on a laptop-sized single node. Queries stay far below this.
    opensearch_timeout_seconds: float = Field(default=30.0, alias="OPENSEARCH_TIMEOUT_SECONDS")
    # Basic-auth credentials for secured deployments; blank (the local default,
    # security plugin disabled) sends no Authorization header.
    opensearch_username: str = Field(default="", alias="OPENSEARCH_USERNAME")
    opensearch_password: str = Field(default="", alias="OPENSEARCH_PASSWORD")

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
    # How many tool-calling turns the grounded answer runtime may take before it
    # forces a final, tool-free synthesis (issue #148 — the agent loop bound; a
    # "turn" is one streamed completion that may request tools). This is the
    # SYSTEM default; a tenant admin may override it per tenant (``Tenant``
    # ``max_tool_turns``). Kept config, not a literal (backend/AGENTS.md: LLM
    # limits are config). Bounded to the same 1–50 band as the per-tenant
    # override so neither path can disable bounding or explode answer cost.
    chat_max_tool_turns: int = Field(default=20, alias="CHAT_MAX_TOOL_TURNS")

    @field_validator("chat_max_tool_turns")
    @classmethod
    def _chat_max_tool_turns_in_band(cls, value: int) -> int:
        """Reject a budget outside 1–50: 0/negative disables bounding, >50 risks cost."""
        if not 1 <= value <= 50:
            raise ValueError("CHAT_MAX_TOOL_TURNS must be between 1 and 50 (issue #148)")
        return value

    # How long (seconds) the app lifespan waits for in-flight answer producers to
    # cancel and drain on shutdown before it stops waiting and proceeds to engine
    # disposal (issue #156). The answer runtime runs off the request as a tracked
    # ``asyncio.Task``; on SIGTERM the lifespan cancels those tasks and awaits them
    # bounded by this budget so a hung/slow answer can no longer block uvicorn's
    # graceful shutdown. Kept config, not a literal at the call site
    # (backend/AGENTS.md). A non-positive value would disable the bound — rejected.
    chat_shutdown_grace_seconds: float = Field(default=10.0, alias="CHAT_SHUTDOWN_GRACE_SECONDS")

    @field_validator("chat_shutdown_grace_seconds")
    @classmethod
    def _chat_shutdown_grace_positive(cls, value: float) -> float:
        """Reject a non-positive grace: 0/negative would disable the shutdown bound."""
        if value <= 0:
            raise ValueError("CHAT_SHUTDOWN_GRACE_SECONDS must be positive (issue #156)")
        return value

    # --- Ingestion (CC-5 / issue #21) ---------------------------------------
    # Chunking is config, not a literal at the call site (backend/AGENTS.md): the
    # target chunk window and the overlap adjacent chunks share, both in
    # characters. Defaults are a reasonable passage size for retrieval; tune per
    # corpus without a code change. Invariant: 0 <= overlap < size (validated).
    ingestion_chunk_size: int = Field(default=1200, alias="INGESTION_CHUNK_SIZE")
    ingestion_chunk_overlap: int = Field(default=200, alias="INGESTION_CHUNK_OVERLAP")
    # How many chunks are embedded per gateway ``embed()`` call. Batching keeps
    # the provider round-trips down (one call per batch instead of per chunk);
    # the gateway sends each batch as a single request preserving order.
    ingestion_embed_batch_size: int = Field(default=64, alias="INGESTION_EMBED_BATCH_SIZE")
    # Celery retry policy for the ingestion task (idempotent, backed off,
    # dead-lettered — backend/AGENTS.md). Max attempts and the base backoff (the
    # task uses exponential backoff capped by Celery's retry_backoff_max).
    ingestion_max_retries: int = Field(default=3, alias="INGESTION_MAX_RETRIES")
    ingestion_retry_backoff_seconds: int = Field(default=5, alias="INGESTION_RETRY_BACKOFF_SECONDS")

    @field_validator("ingestion_chunk_size")
    @classmethod
    def _chunk_size_positive(cls, value: int) -> int:
        """A non-positive chunk window would make ingestion loop/empty — reject."""
        if value <= 0:
            raise ValueError("INGESTION_CHUNK_SIZE must be positive")
        return value

    # --- Connector sync rate limit (ADR-0009 §3, issue #20) -----------------
    # Per-tenant fetch rate limit, enforced at the sync-enqueue boundary
    # (Redis-backed fixed window) so a single tenant cannot make the server fan
    # out unbounded outbound fetches. A sync that would exceed the window is
    # **deferred** (re-enqueued with backoff), never dropped and never surfaced
    # as an HTTP error (the /sources contract is frozen — no 429). The window is
    # ``source_sync_rate_max_per_window`` syncs per ``source_sync_rate_window_seconds``
    # seconds per tenant; a deferred sync re-enqueues after
    # ``source_sync_rate_backoff_seconds`` (bounded by the window).
    source_sync_rate_max_per_window: int = Field(
        default=30, alias="SOURCE_SYNC_RATE_MAX_PER_WINDOW"
    )
    source_sync_rate_window_seconds: int = Field(
        default=60, alias="SOURCE_SYNC_RATE_WINDOW_SECONDS"
    )
    source_sync_rate_backoff_seconds: int = Field(
        default=30, alias="SOURCE_SYNC_RATE_BACKOFF_SECONDS"
    )

    @field_validator(
        "source_sync_rate_max_per_window",
        "source_sync_rate_window_seconds",
        "source_sync_rate_backoff_seconds",
    )
    @classmethod
    def _source_sync_rate_positive(cls, value: int) -> int:
        """A non-positive rate window/limit/backoff would disable bounding — reject.

        The per-tenant fetch rate limit is load-bearing (ADR-0009 §3); a zero or
        negative value would either divide-by-window-zero or make every sync
        defer forever, so misconfiguration must fail fast at startup.
        """
        if value <= 0:
            raise ValueError(
                "SOURCE_SYNC_RATE_* (max/window/backoff) must be positive (ADR-0009 §3)"
            )
        return value

    # --- Web connector outbound identity (ADR-0009 §3, issue #138) -----------
    # Descriptive User-Agent sent on EVERY outbound web-connector fetch. Many
    # sites (e.g. Wikimedia) reject a request that announces no descriptive
    # client with a 4xx error *page*; without a real UA the connector would index
    # that rejection page as if it were the article. Left blank it is filled in
    # below with a version-stamped token + contact URL; override the whole string
    # via WEB_USER_AGENT (e.g. to carry an ops contact for a specific deployment).
    web_user_agent: str = Field(default="", alias="WEB_USER_AGENT")

    @model_validator(mode="after")
    def _default_web_user_agent(self) -> Settings:
        """Fill a descriptive, version-stamped default UA when none is configured.

        A blank ``WEB_USER_AGENT`` (the default) becomes
        ``LumenCopilot/<version> (+<repo-url>)`` so a fresh deploy is already a
        well-behaved client; an explicitly configured value is preserved verbatim.
        """
        if not self.web_user_agent.strip():
            self.web_user_agent = (
                f"LumenCopilot/{self.version} (+https://github.com/k-sandhu/lumen-copilot)"
            )
        return self

    @model_validator(mode="after")
    def _validate_chunk_overlap(self) -> Settings:
        """Enforce ``0 <= overlap < size`` so chunking always makes progress."""
        if self.ingestion_chunk_overlap < 0:
            raise ValueError("INGESTION_CHUNK_OVERLAP must be >= 0")
        if self.ingestion_chunk_overlap >= self.ingestion_chunk_size:
            raise ValueError("INGESTION_CHUNK_OVERLAP must be < INGESTION_CHUNK_SIZE")
        return self

    # --- Chat-model picker registry (issue #47) ---
    # The curated set the picker offers (GET /models), grouped by tier. Config,
    # not a router constant (AC-2): the default seed lives in code as the typed
    # _DEFAULT_CHAT_MODEL_REGISTRY; override the whole list via the
    # CHAT_MODEL_REGISTRY env var as a JSON array of objects matching
    # ChatModelSetting. Invariant (AC-1): non-empty, with EXACTLY ONE is_default.
    chat_model_registry: tuple[ChatModelSetting, ...] = Field(
        default=_DEFAULT_CHAT_MODEL_REGISTRY,
        alias="CHAT_MODEL_REGISTRY",
    )

    @field_validator("chat_model_registry", mode="before")
    @classmethod
    def _parse_chat_model_registry(cls, value: object) -> object:
        """Accept a JSON-array env string as the registry override.

        pydantic-settings hands complex env values through as raw strings; parse
        a ``CHAT_MODEL_REGISTRY`` JSON array here so each element is then
        validated against :class:`ChatModelSetting`. A blank string falls back to
        the default seed (treated as "unset").
        """
        if isinstance(value, str):
            if not value.strip():
                return _DEFAULT_CHAT_MODEL_REGISTRY
            return json.loads(value)
        return value

    @field_validator("chat_model_registry")
    @classmethod
    def _registry_has_exactly_one_default(
        cls, value: tuple[ChatModelSetting, ...]
    ) -> tuple[ChatModelSetting, ...]:
        """Enforce the registry invariants (issue #47 AC-1).

        Non-empty, unique ids, and **exactly one** ``is_default`` — so the picker
        always has a well-defined default and the contract's "exactly one
        ``is_default``" holds for any (mis)configuration. A bad override fails at
        startup, not deep in a request.
        """
        if not value:
            raise ValueError("CHAT_MODEL_REGISTRY must list at least one model")
        ids = [m.id for m in value]
        if len(ids) != len(set(ids)):
            raise ValueError("CHAT_MODEL_REGISTRY must not contain duplicate model ids")
        defaults = [m for m in value if m.is_default]
        if len(defaults) != 1:
            raise ValueError(
                "CHAT_MODEL_REGISTRY must mark EXACTLY ONE model is_default "
                f"(found {len(defaults)})"
            )
        return value

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
