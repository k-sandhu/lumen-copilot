"""Shared test fixtures.

Sets the minimum environment so ``Settings`` (the only env reader) constructs
without a real ``.env`` / live stack, then clears the settings cache so the test
environment is the one in effect. Keeps tests dependency-light: no Postgres,
Redis, or MinIO is required to import the app or hit ``/health``.
"""

from __future__ import annotations

import os

import pytest

# Minimal valid environment for Settings. Values are syntactically valid URLs
# but point nowhere — readiness checks (which DO reach out) are not exercised by
# the unit/API tests here, so nothing actually connects.
_TEST_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "test",
    "S3_SECRET_KEY": "test_secret",
    "S3_BUCKET": "test-bucket",
    "ENVIRONMENT": "local",
    "LOG_LEVEL": "info",
    "OPENROUTER_API_KEY": "",
}


@pytest.fixture(autouse=True, scope="session")
def _test_environment() -> None:
    """Populate the env and reset the cached settings singleton once."""
    for key, value in _TEST_ENV.items():
        os.environ.setdefault(key, value)

    from app.core.config import get_settings

    get_settings.cache_clear()
