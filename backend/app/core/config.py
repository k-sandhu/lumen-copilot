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

    # --- Secrets vault master key (CC / issue #209, spec 0004 "deny by default") ---
    # Base64 of a 32-byte (AES-256) master key for the per-tenant secrets vault's
    # envelope encryption (``app.core.crypto``). A dev default lets the skeleton
    # boot locally; OUTSIDE ``local`` the app refuses to start unless this is
    # overridden — the same fail-fast rule as ``JWT_SECRET`` (validator below), so
    # a deployed vault never encrypts under a publicly-known key. The value is
    # validated to be base64 of exactly 32 bytes at construction (fail fast) rather
    # than deep in a store/retrieve call.
    secrets_encryption_key: str = Field(
        default="bHVtZW4tbG9jYWwtZGV2LXNlY3JldHMta2V5LTAwMDA=",
        alias="SECRETS_ENCRYPTION_KEY",
    )

    # --- Managed-connector OAuth (ADR-0019 §1, issue #452) ---
    # TTL of the server-side single-use state record. Bounded [1, 600]: the ADR
    # caps the flow at 10 minutes — the record holds the PKCE verifier, so an
    # overlong TTL retains it beyond the decided window, and a non-positive TTL
    # would fail deep in Redis instead of at boot (fail fast, INV-8).
    connector_oauth_state_ttl_seconds: int = Field(
        default=600, ge=1, le=600, alias="CONNECTOR_OAUTH_STATE_TTL_SECONDS"
    )
    # Externally-reachable base of THIS API — the provider redirects the browser
    # to ``{base}/api/v1/sources/oauth/callback``, so it must be the URL the
    # browser (and the provider's allowlist) sees, not an in-network address.
    # Default = the local compose host port (ADR-0005).
    connector_oauth_redirect_base_url: str = Field(
        default="http://localhost:47181", alias="CONNECTOR_OAUTH_REDIRECT_BASE_URL"
    )
    # The SPA sources route the callback 302s back to (the contract's frozen
    # ``{return_url}?connect=...`` target). Default = the local compose SPA.
    connector_oauth_frontend_return_url: str = Field(
        default="http://localhost:47180/sources",
        alias="CONNECTOR_OAUTH_FRONTEND_RETURN_URL",
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
    # Browser-reachable base URL used ONLY for minting presigned URLs (#241).
    # SigV4 binds the signature to the Host header, so presigning must happen
    # against the URL the client will actually fetch — inside compose that
    # differs from the in-network S3_ENDPOINT_URL (http://minio:9000 internally
    # vs the published host port from a browser). Unset ⇒ presign against
    # S3_ENDPOINT_URL (single-network deployments where one URL serves both).
    s3_public_endpoint_url: str | None = Field(default=None, alias="S3_PUBLIC_ENDPOINT_URL")

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

    # --- Artifact store (CC-12 / issue #208) --------------------------------
    # Files agents/runs *produce* (distinct from uploaded documents): stored via
    # the same ObjectStore under an ``artifacts/`` prefix, but with their own cap
    # and a **broader** allowlist (agent output is more varied than an upload).
    # Hard upper bound on a single produced artifact (bytes). Default 50 MiB.
    # Validated before storing so an over-cap artifact is a typed 422, never a
    # silent drop or a 500 (#208 AC-2).
    max_artifact_bytes: int = Field(default=50 * 1024 * 1024, alias="MAX_ARTIFACT_BYTES")
    # Allowlisted artifact content-types (#208 AC-2). Broader than the upload set:
    # also csv/json/png/svg/xlsx/docx/pptx/md/txt/html — the formats a file-writing
    # tool or code sandbox typically emits. The declared type is checked against
    # this set before storing (a client-declared type is a usability/allowlist
    # check, not a security guarantee — sniffing is fenced OUT, OD-4). Comma-
    # separated override via ARTIFACT_ALLOWED_CONTENT_TYPES.
    artifact_allowed_content_types: frozenset[str] = Field(
        default=frozenset(
            {
                "text/csv",
                "application/json",
                "image/png",
                "image/svg+xml",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # xlsx
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # docx
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # pptx
                "text/markdown",
                "text/plain",
                "text/html",
            }
        ),
        alias="ARTIFACT_ALLOWED_CONTENT_TYPES",
    )
    # Default retention window for a produced artifact, in days. NULL/absent (the
    # default) ⇒ **keep** (no expiry); a positive value stamps
    # ``retention_expires_at = created_at + N days`` at creation, and the retention
    # janitor (``app.tasks.artifact_retention``) may purge rows past it. Kept
    # config, not a literal at the call site (backend/AGENTS.md).
    artifact_retention_days: int | None = Field(default=None, alias="ARTIFACT_RETENTION_DAYS")

    @field_validator("artifact_allowed_content_types", mode="before")
    @classmethod
    def _split_artifact_content_types(cls, value: object) -> object:
        """Accept a comma-separated env string as the artifact content-type allowlist."""
        if isinstance(value, str):
            return frozenset(item.strip() for item in value.split(",") if item.strip())
        return value

    # --- Per-tenant application logo (admin branding) -----------------------
    # A tenant ADMIN uploads a brand mark that replaces the default "Lumen /
    # Copilot" wordmark in the app shell for every user of that tenant. Stored via
    # the same ObjectStore as uploads/artifacts (its own small cap + a tight
    # image-only allowlist), the object key persisted on the ``tenants`` row. A
    # logo is chrome, not a document, so it gets a much smaller cap. The declared
    # type is checked against this set before storing (a client-declared type is a
    # usability/allowlist check, not a security guarantee — sniffing is fenced OUT,
    # OD-4). Hard upper bound on a single logo (bytes). Default 1 MiB.
    max_logo_bytes: int = Field(default=1 * 1024 * 1024, alias="MAX_LOGO_BYTES")
    # Allowlisted logo content-types: the raster + vector marks a browser renders
    # inline. Comma-separated override via LOGO_ALLOWED_CONTENT_TYPES.
    logo_allowed_content_types: frozenset[str] = Field(
        default=frozenset({"image/png", "image/jpeg", "image/svg+xml"}),
        alias="LOGO_ALLOWED_CONTENT_TYPES",
    )

    @field_validator("logo_allowed_content_types", mode="before")
    @classmethod
    def _split_logo_content_types(cls, value: object) -> object:
        """Accept a comma-separated env string as the logo content-type allowlist."""
        if isinstance(value, str):
            return frozenset(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("artifact_retention_days")
    @classmethod
    def _artifact_retention_days_positive(cls, value: int | None) -> int | None:
        """A retention window must be positive when set; ``None`` = keep forever.

        A zero/negative window would either purge every artifact immediately or be
        meaningless, so a misconfiguration fails fast at startup rather than
        silently deleting agent output (#208).
        """
        if value is not None and value <= 0:
            raise ValueError("ARTIFACT_RETENTION_DAYS must be a positive number of days, or unset")
        return value

    # When true (default), GET /documents/{id}/content responds 302 to a
    # short-TTL presigned GET URL (CC-12, the contract's primary path) so the
    # bytes transfer directly from storage, not through the API process. When
    # false, the API streams the bytes inline (application/octet-stream) — useful
    # where a redirect is undesirable (e.g. same-origin embedding). Both are
    # contract-valid (the 200 and 302 responses are both defined).
    document_content_redirect: bool = Field(default=True, alias="DOCUMENT_CONTENT_REDIRECT")
    # Cap on the extracted text served by GET /documents/{id}/text (#244), in
    # UTF-8 bytes. The viewer needs readable text, not an unbounded payload —
    # an over-cap document is cut at a character boundary and flagged
    # ``truncated`` so the UI says so honestly. Default 2 MiB.
    document_text_max_bytes: int = Field(default=2 * 1024 * 1024, alias="DOCUMENT_TEXT_MAX_BYTES")

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

    # #395 — operational/cost controls for the search path (config-driven per
    # backend/AGENTS.md: limits are never hardcoded at call sites).
    # Query-embedding cache (single-text, default-credential, namespaced calls).
    llm_embed_cache_max_entries: int = Field(default=512, gt=0, alias="LLM_EMBED_CACHE_MAX_ENTRIES")
    llm_embed_cache_ttl_seconds: float = Field(
        default=900.0, gt=0, alias="LLM_EMBED_CACHE_TTL_SECONDS"
    )
    # Output ceiling for the short cited direct answer on /search.
    search_direct_answer_max_tokens: int = Field(
        default=300, gt=0, alias="SEARCH_DIRECT_ANSWER_MAX_TOKENS"
    )
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

    # Follow-up suggestions after an answer (spec 0006, #429): one cheap extra
    # completion on the session's resolved route, emitted as event:suggestions.
    # A nicety, so it is config-gated and time-bounded; any failure is a silent
    # skip — an answer never degrades because suggestions did. Count is capped
    # at the contract's ChatSuggestions maxItems (5).
    chat_suggestions_enabled: bool = Field(default=True, alias="CHAT_SUGGESTIONS_ENABLED")
    chat_suggestions_count: int = Field(default=3, ge=1, le=5, alias="CHAT_SUGGESTIONS_COUNT")
    chat_suggestions_timeout_seconds: float = Field(
        default=8.0, gt=0, le=60, alias="CHAT_SUGGESTIONS_TIMEOUT_SECONDS"
    )

    # Rolling session summary (#416, ADR-0016 §3.2): the async post-answer
    # summarizer. ``keep_messages`` is the verbatim tail never summarized (the
    # last M turns stay word-for-word); ``min_batch`` is how many messages
    # beyond that tail must accumulate before a summarize call is worth its
    # cost (the task no-ops below it). ``summary_model`` pins the summarizer's
    # model; empty ⇒ the registry default (tasks run headless with config
    # credentials — per-tenant ``provider:`` ids are not routable there).
    chat_summary_enabled: bool = Field(default=True, alias="CHAT_SUMMARY_ENABLED")
    chat_summary_keep_messages: int = Field(
        default=8, ge=2, le=50, alias="CHAT_SUMMARY_KEEP_MESSAGES"
    )
    chat_summary_min_batch: int = Field(default=4, ge=1, le=50, alias="CHAT_SUMMARY_MIN_BATCH")
    chat_summary_model: str = Field(default="", alias="CHAT_SUMMARY_MODEL")

    # Prompt caching (ADR-0016 §2, #411): provider cache directives on the
    # answer loop's repeated prefixes (Anthropic cache_control breakpoints /
    # OpenAI prompt_cache_key). A kill-switch, not a tuning knob — off means
    # the exact pre-#411 wire shape everywhere.
    chat_prompt_cache_enabled: bool = Field(default=True, alias="CHAT_PROMPT_CACHE_ENABLED")

    # Context-assembler budget knobs (ADR-0016 §1, issue #410). The conservative
    # input-window used when the model is unknown to the local model map, and the
    # tokens reserved for the completion. Config, not literals (backend/AGENTS.md).
    # Bounded so a bad value fails at startup, not as a silent guard bypass (#424
    # review, finding 6): a non-positive fallback would floor the budget to a
    # confusing 1-token refusal, and a NEGATIVE headroom would INFLATE the input
    # budget beyond the model's real window — defeating the overflow guard.
    context_fallback_max_input_tokens: int = Field(
        default=100_000, gt=0, alias="CONTEXT_FALLBACK_MAX_INPUT_TOKENS"
    )
    context_output_headroom_tokens: int = Field(
        default=8_000, ge=0, alias="CONTEXT_OUTPUT_HEADROOM_TOKENS"
    )
    # In-answer tool-result compaction knobs (ADR-0016 §3.1, issue #415): the
    # chars of a tool result's real content the digest keeps, and how many results
    # one compaction pass clears (a chunk, so cache invalidation is amortized).
    # Both positive so a bad value fails at startup rather than degrading silently.
    context_compaction_digest_chars: int = Field(
        default=1200, gt=0, alias="CONTEXT_COMPACTION_DIGEST_CHARS"
    )
    context_compaction_chunk_size: int = Field(
        default=4, gt=0, alias="CONTEXT_COMPACTION_CHUNK_SIZE"
    )
    # How many of one turn's read-only tool calls execute at once (#412,
    # ADR-0016 §5). Each concurrently EXECUTING call briefly opens its own DB
    # session (released before it queues to persist), so this bounds the
    # per-answer draw on the engine pool — keep it under the pool size, and
    # remember concurrent answers each get their own batch. Validated to
    # [1, 16]: 0 would deadlock the batch semaphore, an unbounded value could
    # exhaust the pool; 1 disables fan-out entirely (the genuinely serial
    # pre-#412 path — no batch, no extra sessions, per-call event order).
    chat_tool_concurrency: int = Field(default=4, gt=0, le=16, alias="CHAT_TOOL_CONCURRENCY")

    @model_validator(mode="after")
    def _context_budget_leaves_room(self) -> Settings:
        """The fallback window must leave positive input room after headroom + margin.

        The assembler computes ``budget = fallback − headroom − safety_margin``
        (the margin absorbs tokenizer drift). This rejects a config where that
        derived budget is not strictly positive — e.g. fallback 1025 + headroom
        1024, which passes the field bounds yet floors the budget to 1 and refuses
        even an empty prompt (#424 re-review). The margin is mirrored from
        ``app.llm.context._SAFETY_MARGIN_TOKENS`` (kept in lockstep — both are the
        same 1024-token drift allowance).
        """
        _context_safety_margin = 1024
        derived_budget = (
            self.context_fallback_max_input_tokens
            - self.context_output_headroom_tokens
            - _context_safety_margin
        )
        if derived_budget <= 0:
            raise ValueError(
                "CONTEXT_FALLBACK_MAX_INPUT_TOKENS must exceed "
                "CONTEXT_OUTPUT_HEADROOM_TOKENS by more than the 1024-token safety "
                "margin so a positive input budget remains (issue #410)"
            )
        return self

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

    # --- Web search (the ``web_search`` agent tool — ADR-0014, issue #219) ----
    # Backs the ``web_search`` tool with self-hosted SearXNG (OSS, no per-query
    # key; ADR-0014 §1) run as a compose service. **Off by default** (governance,
    # ADR-0014 §5): the tool fails closed unless a tenant/deploy explicitly enables
    # web mode here — a disabled deploy returns a tool *result* error, never a
    # crash. The endpoint is an internal service address, so the query leg is a
    # trusted internal hop (distinct from the untrusted result-page fetch, which
    # always goes through the ``connectors/web/fetch.py`` SSRF chokepoint).
    web_search_enabled: bool = Field(default=False, alias="WEB_SEARCH_ENABLED")
    # SearXNG JSON endpoint the adapter queries. Inside compose this is the service
    # name; on the host (tests/dev outside compose) the default targets the
    # compose-mapped host port so a live probe reaches the same engine.
    web_search_endpoint: str = Field(default="http://localhost:47187", alias="WEB_SEARCH_ENDPOINT")
    # Per-request wall-clock budget for the query leg (adapter -> SearXNG). A
    # stalled provider surfaces as a tool timeout, never a hung run.
    web_search_timeout_seconds: float = Field(default=10.0, alias="WEB_SEARCH_TIMEOUT_SECONDS")
    # Default number of results returned when the model does not specify ``k``.
    web_search_default_k: int = Field(default=5, alias="WEB_SEARCH_DEFAULT_K")
    # Hard cap on the requested ``k`` (a hostile/large value is clamped to this).
    web_search_max_k: int = Field(default=10, alias="WEB_SEARCH_MAX_K")
    # How many top result pages to fetch + extract through the SSRF chokepoint for
    # passage-level, cite-worthy text (ADR-0014 §4). ``0`` disables page fetching
    # (snippets only). Each fetched page counts against the per-tenant rate limit.
    web_search_fetch_top_n: int = Field(default=3, alias="WEB_SEARCH_FETCH_TOP_N")
    # Per-tenant rate limit for web search, reusing the Redis fixed-window limiter
    # (``tasks/rate_limit.py``; ADR-0014 §3). Bounds how many search calls one
    # tenant may make per window so a single tenant cannot fan out unbounded
    # outbound requests (search calls + result-page fetches) — a DoS/amplification
    # pivot. An over-budget search is refused as a tool result (throttled), not an
    # HTTP error. Distinct keyspace from the connector-sync limiter.
    web_search_rate_max_per_window: int = Field(default=20, alias="WEB_SEARCH_RATE_MAX_PER_WINDOW")
    web_search_rate_window_seconds: int = Field(default=60, alias="WEB_SEARCH_RATE_WINDOW_SECONDS")

    @field_validator(
        "web_search_default_k",
        "web_search_max_k",
        "web_search_rate_max_per_window",
        "web_search_rate_window_seconds",
    )
    @classmethod
    def _web_search_counts_positive(cls, value: int) -> int:
        """A non-positive k/window/limit would disable bounding — reject (fail fast).

        The per-tenant web-search rate limit is load-bearing (ADR-0014 §3); a zero
        or negative window/limit would make the limiter admit everything or divide
        by a zero window, and a non-positive ``k`` would make a search return
        nothing, so a misconfiguration must fail fast at startup.
        """
        if value <= 0:
            raise ValueError(
                "WEB_SEARCH_* (default_k/max_k/rate max/window) must be positive (ADR-0014)"
            )
        return value

    @field_validator("web_search_fetch_top_n")
    @classmethod
    def _web_search_fetch_top_n_non_negative(cls, value: int) -> int:
        """Result-page fetch count must be >= 0 (``0`` = snippets only, no fetch)."""
        if value < 0:
            raise ValueError("WEB_SEARCH_FETCH_TOP_N must be >= 0 (0 disables page fetching)")
        return value

    # --- MCP client adapter + egress (ADR-0012, issue #225) ------------------
    # The remote MCP client (``app/mcp/``) connects to user-supplied MCP server
    # endpoints, so — like the web connector — every outbound connection is
    # SSRF-guarded and per-tenant rate-limited (ADR-0012 §4). ALL knobs are config,
    # never a literal at the call site (backend/AGENTS.md).
    #
    # Which remote transports the adapter will open. Only the remote transports
    # ship in v1 (ADR-0012 §1); stdio/local-process is deferred behind the
    # code-execution sandbox and is not even a valid value. Comma-separated
    # override; an unknown transport name fails fast at startup.
    mcp_allowed_transports: frozenset[str] = Field(
        default=frozenset({"streamable_http", "sse"}),
        alias="MCP_ALLOWED_TRANSPORTS",
    )
    # Per-connect wall-clock budget (adapter -> MCP server). A stalled server
    # surfaces as a contained ``mcp_timeout`` result, never a hung run.
    mcp_connect_timeout_seconds: float = Field(default=15.0, alias="MCP_CONNECT_TIMEOUT_SECONDS")
    # Per-call wall-clock budget for one tool invocation / discovery / probe.
    mcp_call_timeout_seconds: float = Field(default=30.0, alias="MCP_CALL_TIMEOUT_SECONDS")
    # Per-tenant MCP egress rate limit, reusing the Redis fixed-window limiter
    # (``tasks/rate_limit.py``; ADR-0012 §4). Bounds how many MCP connections one
    # tenant may open per window so a single tenant cannot fan out unbounded
    # outbound requests. Over-budget → a contained ``mcp_rate_limited`` result,
    # never an HTTP error. Distinct keyspace from the connector-sync / web-search
    # / run limiters.
    mcp_rate_max_per_window: int = Field(default=30, alias="MCP_RATE_MAX_PER_WINDOW")
    mcp_rate_window_seconds: int = Field(default=60, alias="MCP_RATE_WINDOW_SECONDS")
    # Optional admin endpoint allowlist (ADR-0012 §4 — defence-in-depth). A
    # comma-separated set of permitted MCP endpoint hosts; **empty = no allowlist**
    # (the SSRF guard is the mandatory control). An allowlist only *narrows*
    # (deny-by-default on top of SSRF), never widens — an allowlisted host still
    # passes the full range check. Not required for v1.
    mcp_endpoint_allowlist: frozenset[str] = Field(
        default=frozenset(), alias="MCP_ENDPOINT_ALLOWLIST"
    )

    @field_validator("mcp_allowed_transports", "mcp_endpoint_allowlist", mode="before")
    @classmethod
    def _split_mcp_sets(cls, value: object) -> object:
        """Accept a comma-separated env string for the MCP transport/allowlist sets."""
        if isinstance(value, str):
            return frozenset(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("mcp_allowed_transports")
    @classmethod
    def _mcp_transports_known(cls, value: frozenset[str]) -> frozenset[str]:
        """Reject any transport that is not a shipped remote transport (ADR-0012 §1).

        Only ``streamable_http`` / ``sse`` are valid; ``stdio`` (or anything else)
        is deferred and must not be configurable — a misconfiguration fails fast at
        startup rather than letting an unshippable transport be requested.
        """
        allowed = {"streamable_http", "sse"}
        unknown = value - allowed
        if unknown:
            raise ValueError(
                f"MCP_ALLOWED_TRANSPORTS may only contain {sorted(allowed)} "
                f"(remote only, ADR-0012 §1); rejected: {sorted(unknown)}"
            )
        if not value:
            raise ValueError("MCP_ALLOWED_TRANSPORTS must list at least one transport")
        return value

    @field_validator(
        "mcp_connect_timeout_seconds",
        "mcp_call_timeout_seconds",
        "mcp_rate_max_per_window",
        "mcp_rate_window_seconds",
    )
    @classmethod
    def _mcp_bounds_positive(cls, value: float) -> float:
        """A non-positive MCP timeout/rate would disable a bound — reject (fail fast).

        The per-call timeouts and the per-tenant egress rate limit are load-bearing
        (ADR-0012 §4/§7); a zero or negative value would let a call run unbounded or
        make the limiter admit everything, so a misconfiguration fails fast.
        """
        if value <= 0:
            raise ValueError(
                "MCP_CONNECT_TIMEOUT_SECONDS / MCP_CALL_TIMEOUT_SECONDS / MCP_RATE_* "
                "must be positive (ADR-0012 §4/§7)"
            )
        return value

    # --- Dynamic per-tenant scheduler (ADR-0015 §7, issue #236) -------------
    # celery-redbeat rides the EXISTING Redis broker (no new infra): the derived
    # live schedule entries live under this key prefix, and Beat holds a leader lock
    # so only one Beat process fires (safe to run a single ``beat`` service). All
    # config, never a literal at the call site (backend/AGENTS.md).
    redbeat_key_prefix: str = Field(default="lumen:redbeat:", alias="REDBEAT_KEY_PREFIX")
    redbeat_lock_timeout_seconds: int = Field(default=90, alias="REDBEAT_LOCK_TIMEOUT_SECONDS")
    # Per-tenant run enqueue rate cap (ADR-0015 §5) — reuses the Redis fixed-window
    # limiter pattern (``tasks/rate_limit.py``). A tenant cannot flood the worker
    # pool with scheduled/run-now runs: the first N enqueues per window are admitted;
    # beyond the cap a fire is deferred with backoff (never dropped). Distinct
    # keyspace from the connector-sync + web-search limiters.
    run_rate_max_per_window: int = Field(default=60, alias="RUN_RATE_MAX_PER_WINDOW")
    run_rate_window_seconds: int = Field(default=60, alias="RUN_RATE_WINDOW_SECONDS")
    run_rate_backoff_seconds: int = Field(default=30, alias="RUN_RATE_BACKOFF_SECONDS")
    # Per-tenant simultaneous in-flight run cap (ADR-0015 §5): bounds how many runs a
    # single tenant may have ``queued``/``running`` at once so one tenant cannot
    # monopolize the worker pool. A fire that would exceed it is deferred, not
    # dropped. 0 would disable the cap → rejected (fail fast).
    run_max_in_flight_per_tenant: int = Field(default=20, alias="RUN_MAX_IN_FLIGHT_PER_TENANT")
    # How often the digest beat rolls pending low-urgency run deliveries into an
    # in-app digest (ADR-0015 §6, issue #238). A completed run whose schedule opted
    # into a digest lands as a ``pending`` delivery; the periodic sweep marks the
    # batch ``delivered`` so the owner is notified once per window, not per fire. The
    # default is hourly (3600s) — the sweep is idempotent and cheap; the *cadence*
    # (daily/weekly) the schedule opted into is a product notion, this is just how
    # often the beat drains the pending batch. Non-positive would disable batching.
    run_digest_interval_seconds: int = Field(default=3600, alias="RUN_DIGEST_INTERVAL_SECONDS")
    # Bounded retry-with-backoff for a **transient** run fault (model/db/storage
    # briefly unavailable) before the run reaches a terminal (ADR-0015 §5, E7-5 #239).
    # A transient failure is re-driven up to ``run_max_retries`` times with exponential
    # backoff (``run_retry_backoff_seconds * 2**attempt``); on exhaustion the run
    # reaches a queryable ``failed`` terminal, never a silent drop. A *permanent* /
    # escalation-worthy failure (ambiguity / restricted data / tool failure) is never
    # retried — it escalates to a human immediately. Mirrors ``ingestion_*``.
    run_max_retries: int = Field(default=3, alias="RUN_MAX_RETRIES")
    run_retry_backoff_seconds: int = Field(default=5, alias="RUN_RETRY_BACKOFF_SECONDS")

    @field_validator(
        "redbeat_lock_timeout_seconds",
        "run_rate_max_per_window",
        "run_rate_window_seconds",
        "run_rate_backoff_seconds",
        "run_max_in_flight_per_tenant",
        "run_digest_interval_seconds",
    )
    @classmethod
    def _scheduler_counts_positive(cls, value: int) -> int:
        """A non-positive lock/rate/concurrency value would disable bounding — reject.

        The per-tenant run rate + concurrency caps are load-bearing availability
        controls (ADR-0015 §5); a zero or negative window/limit/lock would either
        admit everything, divide by a zero window, or disable the Beat lock, so a
        misconfiguration must fail fast at startup.
        """
        if value <= 0:
            raise ValueError(
                "REDBEAT_LOCK_TIMEOUT_SECONDS / RUN_RATE_* / RUN_MAX_IN_FLIGHT_PER_TENANT "
                "/ RUN_DIGEST_INTERVAL_SECONDS must be positive (ADR-0015 §5/§6/§7)"
            )
        return value

    @model_validator(mode="after")
    def _validate_chunk_overlap(self) -> Settings:
        """Enforce ``0 <= overlap < size`` so chunking always makes progress."""
        if self.ingestion_chunk_overlap < 0:
            raise ValueError("INGESTION_CHUNK_OVERLAP must be >= 0")
        if self.ingestion_chunk_overlap >= self.ingestion_chunk_size:
            raise ValueError("INGESTION_CHUNK_OVERLAP must be < INGESTION_CHUNK_SIZE")
        return self

    # --- Sandbox code execution (ADR-0020, issue #457) -----------------------
    # The isolated Python code-execution sandbox — the HIGHEST-RISK capability in
    # the program (adversarial-by-assumption model-authored code). ALL settings are
    # config, never a literal at the call site (backend/AGENTS.md).
    #
    # **Default OFF per tenant (ADR-0013 §6, the kill-switch).** Code execution is
    # disabled for every tenant until an admin explicitly enables it. The system flag
    # below is the deploy-wide master switch (default False): with it off, EVERY run
    # is refused (``status=denied``, audited) — the sandbox never launches. The
    # per-tenant admin enable (#233) layers on top; this issue ships the master switch
    # and defaults them closed.
    sandbox_enabled: bool = Field(default=False, alias="SANDBOX_ENABLED")
    # The internal HTTP API the worker calls the dedicated ``sandbox-runner`` service
    # on (ADR-0013 §1). Inside compose this is the service name on the internal
    # network, never a published host port. The worker holds NO Docker socket — this
    # hop is the entire container-engine surface.
    sandbox_runner_url: str = Field(
        default="http://sandbox-runner:8000", alias="SANDBOX_RUNNER_URL"
    )
    # The pinned base image the sandbox runs (curated Python + scientific stack,
    # ADR-0013 §3). Pinned by digest, no ``:latest``. The runner uses this; recorded
    # per run for reproducibility (E3-7).
    sandbox_image: str = Field(
        default="lumen-sandbox-runner:0.2.0",
        alias="SANDBOX_IMAGE",
    )
    # The OCI runtime: ``runc`` (hardened Docker baseline, laptop-viable) or ``runsc``
    # (gVisor — the recommended production hardening; a config swap, no code change,
    # ADR-0013 §2). Anything else is rejected fail-fast.
    sandbox_runtime: str = Field(default="runc", alias="SANDBOX_RUNTIME")
    # Compatibility-only ADR-0013 settings. Existing admin-policy rows and API clients
    # still read these fields, so they remain validated and configurable, but ADR-0020
    # reusable sessions deliberately do NOT pass them to the runner or enforce them.
    sandbox_cpus: float = Field(default=1.0, alias="SANDBOX_CPUS")
    sandbox_memory_bytes: int = Field(default=512 * 1024 * 1024, alias="SANDBOX_MEMORY_BYTES")
    sandbox_pids_limit: int = Field(default=128, alias="SANDBOX_PIDS_LIMIT")
    sandbox_wall_clock_seconds: int = Field(default=30, alias="SANDBOX_WALL_CLOCK_SECONDS")
    sandbox_output_bytes_cap: int = Field(default=1 * 1024 * 1024, alias="SANDBOX_OUTPUT_BYTES_CAP")
    sandbox_scratch_bytes: int = Field(default=256 * 1024 * 1024, alias="SANDBOX_SCRATCH_BYTES")
    # Compatibility-only tenant quota values; not enforced for ADR-0020 sessions.
    sandbox_max_concurrent_per_tenant: int = Field(
        default=2, alias="SANDBOX_MAX_CONCURRENT_PER_TENANT"
    )
    sandbox_daily_runtime_seconds_per_tenant: int = Field(
        default=3600, alias="SANDBOX_DAILY_RUNTIME_SECONDS_PER_TENANT"
    )

    @field_validator(
        "sandbox_cpus",
        "sandbox_memory_bytes",
        "sandbox_pids_limit",
        "sandbox_wall_clock_seconds",
        "sandbox_output_bytes_cap",
        "sandbox_scratch_bytes",
        "sandbox_max_concurrent_per_tenant",
        "sandbox_daily_runtime_seconds_per_tenant",
    )
    @classmethod
    def _sandbox_limits_positive(cls, value: float) -> float:
        """Keep compatibility-only ADR-0013 policy values syntactically valid."""
        if value <= 0:
            raise ValueError("SANDBOX_* resource caps and quotas must be positive (ADR-0013 §2/§6)")
        return value

    @field_validator("sandbox_runtime")
    @classmethod
    def _sandbox_runtime_known(cls, value: str) -> str:
        """Only ``runc`` (Docker baseline) or ``runsc`` (gVisor) are valid runtimes."""
        if value not in ("runc", "runsc"):
            raise ValueError("SANDBOX_RUNTIME must be 'runc' or 'runsc' (ADR-0013 §2)")
        return value

    @model_validator(mode="after")
    def _sandbox_requires_gvisor_outside_local(self) -> Settings:
        """Root-capable reusable sandboxes require gVisor outside local development."""
        if self.sandbox_enabled and self.environment != "local" and self.sandbox_runtime != "runsc":
            raise ValueError(
                "SANDBOX_RUNTIME must be 'runsc' when SANDBOX_ENABLED=true outside local "
                "development (ADR-0020)"
            )
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
    # The base64 dev vault key baked into the ``secrets_encryption_key`` default —
    # obviously insecure and refused outside ``local`` (mirrors the JWT rule).
    _DEV_SECRETS_KEY = "bHVtZW4tbG9jYWwtZGV2LXNlY3JldHMta2V5LTAwMDA="

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

    @field_validator("secrets_encryption_key")
    @classmethod
    def _secrets_key_is_valid_material(cls, value: str) -> str:
        """Reject a vault key that is not base64 of exactly 32 bytes (fail fast).

        The secrets vault (issue #209) encrypts with AES-256, which needs a 32-byte
        key. Validating the shape here means a malformed ``SECRETS_ENCRYPTION_KEY``
        refuses to boot rather than failing the first store/retrieve — the same
        fail-fast posture as every other required config value. Delegates to the
        crypto module's loader so the one definition of "valid key material" is not
        duplicated.
        """
        # Local import avoids a core→core import cycle at module load and keeps the
        # cipher's key rules the single source of truth for validity.
        from app.core.crypto import SecretsCryptoError, load_master_key

        try:
            load_master_key(value)
        except SecretsCryptoError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def _reject_dev_secrets_key_in_prod(self) -> Settings:
        """Fail fast if the insecure dev vault key leaks into a deployed env.

        Mirrors the ``JWT_SECRET`` rule (issue #209 AC-5): the dev default lets the
        skeleton boot locally, but any non-``local`` environment still carrying it
        is a misconfiguration that must refuse to start rather than encrypt tenant
        credentials under a publicly-known key.
        """
        if self.environment != "local" and self.secrets_encryption_key == self._DEV_SECRETS_KEY:
            raise ValueError(
                "SECRETS_ENCRYPTION_KEY must be overridden outside the local environment "
                "(issue #209)"
            )
        return self

    @model_validator(mode="after")
    def _require_https_oauth_urls_in_prod(self) -> Settings:
        """OAuth state/code must never transit cleartext outside local dev.

        ADR-0019 §1: the callback URL carries the opaque state handle and the
        provider's authorization code. The http defaults exist only for the
        local compose stack; a deployed environment must serve both the
        callback base and the SPA return target over https — refuse to boot
        otherwise (fail fast, the JWT/vault-key rule applied to OAuth).
        """
        if self.environment != "local":
            for value, name in (
                (self.connector_oauth_redirect_base_url, "CONNECTOR_OAUTH_REDIRECT_BASE_URL"),
                (self.connector_oauth_frontend_return_url, "CONNECTOR_OAUTH_FRONTEND_RETURN_URL"),
            ):
                if not value.startswith("https://"):
                    raise ValueError(
                        f"{name} must be https outside the local environment (ADR-0019 §1)"
                    )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the environment is parsed once. Tests can clear the cache via
    ``get_settings.cache_clear()`` to inject a different environment.
    """
    return Settings()  # values come from env/.env (pydantic-settings)
