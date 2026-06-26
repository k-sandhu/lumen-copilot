"""Auth-config guardrails (issue #19, spec 0004 §2.3).

The access-token TTL ceiling (<= 15 min) and the refusal to boot a non-local
environment on the insecure dev JWT secret are security-load-bearing — pin them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

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


def test_overridden_secret_boots_in_production() -> None:
    s = Settings(
        _env_file=None,
        **_BASE,
        ENVIRONMENT="production",
        JWT_SECRET="a-real-production-secret",
    )
    assert s.environment == "production"


def test_version_is_sourced_from_package_single_source() -> None:
    # The served version (/health + OpenAPI title) must derive from the single
    # package source, app.__version__, not a re-typed literal in config.py — so a
    # release bump cannot leave a stale version on the wire (issue #158). This
    # fails if a hardcoded literal is ever reintroduced in the version field.
    import app

    s = Settings(_env_file=None, **_BASE)
    assert s.version == app.__version__
