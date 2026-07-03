"""Shared helpers for the sandbox tests (#230).

Builds the exact :class:`~app.sandbox.spec.RunSpec` the ``SandboxService`` hands the
runner — so the G5 (no-secret-leakage) isolation test asserts against the *real*
curated env the service produces, not a hand-rolled duplicate. Kept out of the test
modules so both the isolation and service suites import one definition.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.config import Settings
from app.sandbox.service import SandboxService
from app.sandbox.spec import RunSpec, StagedInput


def sandbox_settings(**overrides: object) -> Settings:
    """A minimal, valid :class:`Settings` for the sandbox tests (offline-safe).

    Carries the dev creds the test env uses so the G5 leakage test can assert none of
    them reach the run's env. ``SANDBOX_ENABLED`` is True here so the happy-path
    service tests execute; the config default (OFF) is asserted separately.
    """
    base: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "lumen",
        "S3_SECRET_KEY": "lumen_local_dev_secret",
        "S3_BUCKET": "b",
        "OPENROUTER_API_KEY": "",
        "SANDBOX_ENABLED": "true",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def build_service_spec(
    *, code: str, inputs: tuple[StagedInput, ...] = ()
) -> RunSpec:
    """The run spec the service builds for ``code`` — used by the G5 leakage test.

    Constructs a service (its ``_build_spec`` needs no I/O) and returns the spec it
    would hand the runner, so the assertion is against the production curated env.
    """
    service = SandboxService(
        tenant_id=uuid4(),
        owner_id=uuid4(),
        runner=_NullRunner(),
        object_store=object(),  # type: ignore[arg-type]  # unused by _build_spec
        settings=sandbox_settings(),
    )
    return service._build_spec(code, inputs)  # noqa: SLF001 — the seam under test


class _NullRunner:
    """A runner that is never called (``_build_spec`` does not touch it)."""

    async def run(self, spec: RunSpec, *, tmpfs_scratch_bytes: int) -> object:  # pragma: no cover
        raise AssertionError("the runner must not be called by _build_spec")
