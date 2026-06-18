"""Shared test fixtures.

Sets the minimum environment so ``Settings`` (the only env reader) constructs
without a real ``.env`` / live stack, then clears the settings cache so the test
environment is the one in effect. Keeps tests dependency-light: no Postgres,
Redis, or MinIO is required to import the app or hit ``/health``.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

# Windows defaults to the Proactor event loop, whose socket self-pipe transports
# are GC-finalized late and emit a spurious "unclosed transport" ResourceWarning
# that ``filterwarnings = error`` (pyproject) escalates and mis-attributes to an
# unrelated async test. The Selector loop has no such transport. This is a
# test-runtime concern only — uvicorn manages its own loop in production.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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


def _seed_test_environment() -> None:
    """Set the minimum env for ``Settings`` to construct.

    Runs at **import** time (conftest is imported before any test module is
    collected) because some test modules build the app — and therefore read
    ``Settings`` — at module import. Seeding here, not only in a fixture,
    guarantees the env exists before that collection-time import. ``setdefault``
    keeps any value already supplied by the caller's shell / CI.
    """
    for key, value in _TEST_ENV.items():
        os.environ.setdefault(key, value)


# Seed immediately on conftest import, before collection imports any app module.
_seed_test_environment()


@pytest.fixture(autouse=True, scope="session")
def _test_environment() -> None:
    """Re-assert the env and reset the cached settings singleton once."""
    _seed_test_environment()

    from app.core.config import get_settings

    get_settings.cache_clear()
