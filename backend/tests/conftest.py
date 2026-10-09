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
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from tests._loop_helpers import LoopTracker

_loop_tracker = LoopTracker()
_loop_patch = pytest.MonkeyPatch()
_loop_tracker.install(_loop_patch)


def pytest_configure(config: pytest.Config) -> None:
    # A unique root per invocation; xdist creates its own gwN children beneath
    # this root. Respect explicit --basetemp (CI/shared-machine measurements).
    if config.option.basetemp is None:
        import tempfile

        config.option.basetemp = str(Path(tempfile.gettempdir()) / f"lumen-pytest-{uuid4().hex}")


def pytest_unconfigure(config: pytest.Config) -> None:
    _loop_tracker.close_idle()
    _loop_patch.undo()


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
    "ENVIRONMENT": "test",
    # Existing non-local credential/OAuth guards still apply in test. These are
    # synthetic values, not secrets, and do not relax any production validator.
    "JWT_SECRET": "suite-only-jwt-key",
    "SECRETS_ENCRYPTION_KEY": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    "GDRIVE_OAUTH_CLIENT_ID": "suite-only-google-client",
    "GDRIVE_OAUTH_CLIENT_SECRET": "suite-only-google-secret",
    "CONNECTOR_OAUTH_REDIRECT_BASE_URL": "https://api.example.test",
    "CONNECTOR_OAUTH_FRONTEND_RETURN_URL": "https://app.example.test/sources",
    "LOG_LEVEL": "info",
    "OPENROUTER_API_KEY": "",
    # #416: the post-answer summarize enqueue would attempt a real broker
    # publish from API-level tests (unreachable offline; the fail-fast connect
    # still outlives task-drain assertions). The enqueue seam has its own
    # coverage; offline answers skip it.
    "CHAT_SUMMARY_ENABLED": "false",
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
    if os.environ.get("PYTEST_XDIST_WORKER"):
        from tests._live_helpers import worker_database_url

        for key in ("DATABASE_URL", "AUDIT_DENIAL_LIVE_DATABASE_URL"):
            url = os.environ.get(key, "")
            if "/lumentest_" in url:
                os.environ[key] = worker_database_url(url)


# Seed immediately on conftest import, before collection imports any app module.
_seed_test_environment()


def _disable_dotenv_discovery() -> None:
    """Enforce the suite's hermeticity contract against transitive dotenv loads.

    This module's contract (docstring above) is that the suite constructs
    ``Settings`` "without a real ``.env``" — but **litellm** (imported lazily by
    ``app/llm/gateway.py`` on first use) calls python-dotenv's ``load_dotenv()``
    at import, whose default discovery walks UP the directory tree and loads the
    first ``.env`` it finds into ``os.environ`` MID-RUN. From any checkout with
    an ancestor ``.env`` (every ``.claude/worktrees/*`` tree sits under the repo
    root's live-stack ``.env``) that injects real deployment values —
    ``WEB_SEARCH_ENABLED``, ``CHAT_MODEL_REGISTRY``, ``LLM_MODEL``, … — and
    every later ``Settings()`` construction reads them: order-dependent
    failures across unrelated modules (issue #469; the #94 flake class).

    Stubbing the loader BEFORE litellm's ``from dotenv import load_dotenv``
    binds it makes the contract structural: inside the test process, no code
    path may bulk-load an env file. (Production is untouched — this is test
    scaffolding; the app-side posture is #469's follow-up.)
    """
    import dotenv

    dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]
    dotenv.main.load_dotenv = dotenv.load_dotenv


_disable_dotenv_discovery()

# Imports ``app`` and therefore must remain after the hermetic test environment
# is seeded and dotenv discovery is disabled.
from tests._audit_helpers import RecordingDurableAuditTransactions  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _test_environment() -> None:
    """Re-assert the env and reset the cached settings singleton once."""
    _seed_test_environment()

    from app.core.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True, scope="session")
def _sqlite_schema_template(tmp_path_factory: pytest.TempPathFactory) -> None:
    from tests._db_helpers import build_sqlite_template

    build_sqlite_template(tmp_path_factory.mktemp("schema") / "empty.db")


@pytest.fixture
def durable_audit_ledger() -> RecordingDurableAuditTransactions:
    """Per-test denial ledger, physically outside every offline SQL Session."""
    return RecordingDurableAuditTransactions()


@pytest.fixture(autouse=True)
def _wire_offline_durable_audit(
    monkeypatch: pytest.MonkeyPatch,
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> None:
    """Keep offline API/task denials off the fake configured Postgres URL.

    The production provider has its own engine/pool; the unit/API suite instead
    injects this explicit ledger.  It never falls back to the request's
    StaticPool connection, which is exactly the false-positive R1-001 exposed.
    """
    monkeypatch.setattr(
        "app.api.deps.get_durable_audit_transactions",
        lambda settings=None: durable_audit_ledger,
    )
    monkeypatch.setattr(
        "app.db.session.get_durable_audit_transactions",
        lambda settings=None: durable_audit_ledger,
    )


@pytest.fixture(autouse=True)
def _offline_embedding_contract(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep ordinary unit/API startup offline while preserving an explicit gate.

    Contract/preflight tests override this fixture's patch locally. Production
    still performs the real DB/index/provider startup validation.
    """

    from app.core.config import get_settings
    from app.ingestion.contract import (
        mark_embedding_contract_valid,
        reset_embedding_contract_gate,
    )

    reset_embedding_contract_gate()
    mark_embedding_contract_valid(get_settings().embedding_space_fingerprint)

    async def _validated_for_test(settings: object) -> str:
        fingerprint = settings.embedding_space_fingerprint  # type: ignore[attr-defined]
        mark_embedding_contract_valid(fingerprint)
        return fingerprint

    monkeypatch.setattr("app.main.provision_embedding_contract", _validated_for_test)
    yield
    reset_embedding_contract_gate()


@pytest.fixture(autouse=True)
def _close_orphan_event_loops() -> Iterator[None]:
    """Release tracked idle loops' self-pipe sockets before GC can warn (#94)."""
    yield
    _loop_tracker.close_idle()
