"""Auth-config guardrails (issue #19, spec 0004 §2.3; issue #209).

The access-token TTL ceiling (<= 15 min) and the refusal to boot a non-local
environment on the insecure dev JWT secret are security-load-bearing — pin them.
The same fail-fast posture guards the secrets-vault master key (issue #209): a
malformed ``SECRETS_ENCRYPTION_KEY`` never boots, and the dev default is refused
outside ``local`` (mirroring the JWT rule) — pinned here too.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.crypto import generate_master_key

# A minimal valid base env so Settings constructs; individual tests override one
# field to exercise a single guardrail.
_BASE = {
    "DATABASE_URL": "postgresql+asyncpg://t:t@localhost/t",
    "REDIS_URL": "redis://localhost",
    "CELERY_BROKER_URL": "redis://localhost",
    "CELERY_RESULT_BACKEND": "redis://localhost",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "t",
    "S3_SECRET_KEY": "tt",
    "S3_BUCKET": "b",
}

# A valid, non-dev vault key (base64 of 32 random bytes) for the production-boot
# tests, which must override BOTH the JWT secret and the vault key.
_PROD_SECRETS_KEY = generate_master_key()


def test_access_ttl_ceiling_is_enforced() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, ACCESS_TOKEN_TTL_SECONDS=3600)  # > 900


def test_access_ttl_at_ceiling_is_accepted() -> None:
    s = Settings(_env_file=None, **_BASE, ACCESS_TOKEN_TTL_SECONDS=900)
    assert s.access_token_ttl_seconds == 900


def test_dev_jwt_secret_rejected_outside_local() -> None:
    # ENVIRONMENT != local while still carrying the dev default → refuse to boot.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, ENVIRONMENT="production")


def test_dev_jwt_secret_allowed_in_local() -> None:
    s = Settings(_env_file=None, **_BASE, ENVIRONMENT="local")
    # Default dev secret is fine locally; the skeleton boots.
    assert s.jwt_secret


# Every production-mandatory override in one place: the JWT secret + vault key
# (#209), https OAuth URLs and a real Google client registration (ADR-0019 §1,
# #452/#453) — the boot test below proves a fully-configured production env
# constructs; each guard's negative test drops exactly one of these.
_PROD_OVERRIDES = {
    "JWT_SECRET": "a-real-production-secret",
    "SECRETS_ENCRYPTION_KEY": _PROD_SECRETS_KEY,
    "CONNECTOR_OAUTH_REDIRECT_BASE_URL": "https://api.example.test",
    "CONNECTOR_OAUTH_FRONTEND_RETURN_URL": "https://app.example.test/sources",
    "GDRIVE_OAUTH_CLIENT_ID": "prod-google-client-id",
    "GDRIVE_OAUTH_CLIENT_SECRET": "prod-google-client-secret",
}


def test_overridden_secret_boots_in_production() -> None:
    s = Settings(
        _env_file=None,
        **_BASE,
        ENVIRONMENT="production",
        **_PROD_OVERRIDES,
    )
    assert s.environment == "production"


def test_http_oauth_urls_rejected_outside_local() -> None:
    # ADR-0019 §1: state/code never transit cleartext — an http callback base
    # refuses to boot outside local (all other prod overrides supplied).
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **_BASE,
            ENVIRONMENT="production",
            **{**_PROD_OVERRIDES, "CONNECTOR_OAUTH_REDIRECT_BASE_URL": "http://api.example.test"},
        )


def test_blank_gdrive_client_rejected_outside_local() -> None:
    # ADR-0019 §1 (#453): a deployed env without the Google client registration
    # refuses to boot rather than failing deep inside the first connect flow.
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **_BASE,
            ENVIRONMENT="production",
            **{**_PROD_OVERRIDES, "GDRIVE_OAUTH_CLIENT_SECRET": ""},
        )


def test_blank_gdrive_client_allowed_in_local() -> None:
    s = Settings(_env_file=None, **_BASE, ENVIRONMENT="local")
    assert s.gdrive_oauth_client_id == ""  # the local skeleton boots without one


# --- Secrets-vault master key (issue #209 AC-5) ----------------------------


def test_dev_secrets_key_rejected_outside_local() -> None:
    # ENVIRONMENT != local while still carrying the dev vault key → refuse to boot
    # (mirrors the JWT rule). A real JWT secret is supplied so ONLY the vault-key
    # guard can be what fails.
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **_BASE,
            ENVIRONMENT="production",
            JWT_SECRET="a-real-production-secret",
        )


def test_dev_secrets_key_allowed_in_local() -> None:
    s = Settings(_env_file=None, **_BASE, ENVIRONMENT="local")
    # The dev vault key is fine locally; the skeleton boots and the key is usable.
    assert s.secrets_encryption_key


def test_malformed_secrets_key_is_rejected_at_boot() -> None:
    # Not base64 of 32 bytes → refuse to boot (fail fast), even locally, rather
    # than failing the first store/retrieve. "short" is valid base64 but too few
    # bytes once decoded.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, ENVIRONMENT="local", SECRETS_ENCRYPTION_KEY="short")


def test_valid_overridden_secrets_key_boots() -> None:
    s = Settings(
        _env_file=None,
        **_BASE,
        ENVIRONMENT="local",
        SECRETS_ENCRYPTION_KEY=_PROD_SECRETS_KEY,
    )
    assert s.secrets_encryption_key == _PROD_SECRETS_KEY


def test_version_is_sourced_from_package_single_source() -> None:
    # The served version (/health + OpenAPI title) must derive from the single
    # package source, app.__version__, not a re-typed literal in config.py — so a
    # release bump cannot leave a stale version on the wire (issue #158). This
    # fails if a hardcoded literal is ever reintroduced in the version field.
    import app

    s = Settings(_env_file=None, **_BASE)
    assert s.version == app.__version__


# --- Context-assembler budget guardrails (#410 / #424 review, finding 6) ------


def test_context_negative_headroom_rejected() -> None:
    """A NEGATIVE output headroom would inflate the input budget beyond the
    model's window, defeating the overflow guard — reject at startup."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, CONTEXT_OUTPUT_HEADROOM_TOKENS=-1)


def test_context_nonpositive_fallback_rejected() -> None:
    """A non-positive fallback window would floor the budget to a confusing
    1-token refusal — reject at startup."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, CONTEXT_FALLBACK_MAX_INPUT_TOKENS=0)


def test_context_headroom_not_below_window_rejected() -> None:
    """Reserved output headroom >= the fallback input window leaves no room —
    the cross-field validator rejects it."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **_BASE,
            CONTEXT_FALLBACK_MAX_INPUT_TOKENS=8000,
            CONTEXT_OUTPUT_HEADROOM_TOKENS=8000,
        )


def test_context_budget_margin_gap_rejected() -> None:
    """fallback 1025 + headroom 1024 passes the field bounds but leaves only a
    1-token budget after the 1024 safety margin — the cross-field check rejects
    it (#424 re-review, the margin must be part of the guard)."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            **_BASE,
            CONTEXT_FALLBACK_MAX_INPUT_TOKENS=1025,
            CONTEXT_OUTPUT_HEADROOM_TOKENS=1024,
        )


def test_context_valid_budget_accepted() -> None:
    s = Settings(
        _env_file=None,
        **_BASE,
        CONTEXT_FALLBACK_MAX_INPUT_TOKENS=120_000,
        CONTEXT_OUTPUT_HEADROOM_TOKENS=4000,
    )
    assert s.context_fallback_max_input_tokens == 120_000
    assert s.context_output_headroom_tokens == 4000


def test_context_compaction_knobs_reject_nonpositive() -> None:
    """#415: a non-positive digest size or chunk size would silently disable
    compaction (or loop forever) — both fail at startup instead."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, CONTEXT_COMPACTION_DIGEST_CHARS=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, CONTEXT_COMPACTION_CHUNK_SIZE=0)


def test_context_compaction_knobs_accept_valid() -> None:
    s = Settings(
        _env_file=None,
        **_BASE,
        CONTEXT_COMPACTION_DIGEST_CHARS=800,
        CONTEXT_COMPACTION_CHUNK_SIZE=2,
    )
    assert s.context_compaction_digest_chars == 800
    assert s.context_compaction_chunk_size == 2


def test_chat_tool_concurrency_defaults_and_rejects_out_of_range() -> None:
    """#412: the per-turn concurrent tool-call cap defaults to 4 and is
    validated to [1, 16] — zero would deadlock the batch semaphore, an
    unbounded value could exhaust the engine pool; both fail at startup."""
    assert Settings(_env_file=None, **_BASE).chat_tool_concurrency == 4
    assert Settings(_env_file=None, **_BASE, CHAT_TOOL_CONCURRENCY=2).chat_tool_concurrency == 2
    assert Settings(_env_file=None, **_BASE, CHAT_TOOL_CONCURRENCY=16).chat_tool_concurrency == 16
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, CHAT_TOOL_CONCURRENCY=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_BASE, CHAT_TOOL_CONCURRENCY=17)


def test_chat_prompt_cache_kill_switch_defaults_on() -> None:
    """#411: provider prompt-cache directives default ON and are killable via
    ``CHAT_PROMPT_CACHE_ENABLED=false`` (AC-3: off ⇒ the exact pre-#411 wire)."""
    assert Settings(_env_file=None, **_BASE).chat_prompt_cache_enabled is True
    off = Settings(_env_file=None, **_BASE, CHAT_PROMPT_CACHE_ENABLED="false")
    assert off.chat_prompt_cache_enabled is False
