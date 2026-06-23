"""Shared test fixtures.

Sets the minimum environment so ``Settings`` (the only env reader) constructs
without a real ``.env`` / live stack, then clears the settings cache so the test
environment is the one in effect. Keeps tests dependency-light: no Postgres,
Redis, or MinIO is required to import the app or hit ``/health``.
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys
from collections.abc import Iterator

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


@pytest.fixture(autouse=True)
def _close_orphan_event_loops() -> Iterator[None]:
    """Close per-test/-fixture asyncio loops eagerly so no socket leaks (#94).

    Each asyncio event loop on Windows (``SelectorEventLoop``, forced above) owns
    a wakeup **self-pipe socketpair** (two ``127.0.0.1`` loopback sockets). When a
    loop is dropped without ``close()`` — which pytest-asyncio does for some
    scoped-fixture loops under ``asyncio_mode = "auto"`` — that socketpair is only
    released on a *later* cyclic GC pass. If that pass fires mid-test, CPython's
    unraisable hook raises ``PytestUnraisableExceptionWarning: ResourceWarning:
    unclosed socket`` against **whatever test is running then** — the rotating,
    order-dependent flake characterized in issue #94 (every module passes in
    isolation; only the full suite, and only on whatever happens to run next,
    fails).

    Closing orphaned (non-running, non-closed) loops in teardown releases those
    self-pipe sockets at a deterministic point — bounded to this fixture's own
    teardown window — so the suite is order-independent. ``gc.collect()`` first
    makes any just-dropped loop reachable for the sweep. This is a test-runtime
    concern only; production loops are owned by uvicorn. (See also the live-socket
    leg of #94: the LLM live smokes are skipped by default and close LiteLLM's
    HTTP clients in their own teardown — ``app.llm.aclose_litellm_clients``.)
    """
    yield
    gc.collect()
    for obj in gc.get_objects():
        if isinstance(obj, asyncio.AbstractEventLoop):
            try:
                if not obj.is_running() and not obj.is_closed():
                    obj.close()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass
