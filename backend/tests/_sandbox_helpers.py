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
from app.services.sandbox_policy_service import EffectiveSandboxPolicy


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


def effective_policy(
    *,
    enabled: bool = True,
    allowed_packages: tuple[str, ...] = (),
    denied_packages: tuple[str, ...] = (),
    egress_allowed: bool = False,
    egress_allowlist: tuple[str, ...] = (),
    max_runtime_s: int = 30,
    max_memory_mb: int = 512,
    daily_runtime_cap_s: int = 3600,
    max_concurrency: int = 2,
) -> EffectiveSandboxPolicy:
    """A minimal :class:`EffectiveSandboxPolicy` for the sandbox service/spec tests.

    Defaults mirror the config ceiling used by :func:`sandbox_settings`; override any
    field to prove a per-tenant setting flows into the run spec (egress/packages) or the
    gate (quotas). ``enabled=True`` by default so the happy-path spec tests build a spec.
    """
    return EffectiveSandboxPolicy(
        enabled=enabled,
        allowed_packages=allowed_packages,
        denied_packages=denied_packages,
        egress_allowed=egress_allowed,
        egress_allowlist=egress_allowlist,
        max_runtime_s=max_runtime_s,
        max_memory_mb=max_memory_mb,
        daily_runtime_cap_s=daily_runtime_cap_s,
        max_concurrency=max_concurrency,
    )


def build_service_spec(
    *,
    code: str,
    inputs: tuple[StagedInput, ...] = (),
    policy: EffectiveSandboxPolicy | None = None,
) -> RunSpec:
    """The run spec the service builds for ``code`` — used by the G5 leakage test.

    Constructs a service (its ``_build_spec`` needs no I/O) and returns the spec it
    would hand the runner, so the assertion is against the production curated env. The
    effective policy defaults to the deny-by-default egress/packages posture (#233).
    """
    service = SandboxService(
        tenant_id=uuid4(),
        owner_id=uuid4(),
        runner=_NullRunner(),
        object_store=object(),  # type: ignore[arg-type]  # unused by _build_spec
        settings=sandbox_settings(),
    )
    return service._build_spec(  # noqa: SLF001 — the seam under test
        code, inputs, policy or effective_policy()
    )


class _NullRunner:
    """A runner that is never called (``_build_spec`` does not touch it)."""

    async def run(self, spec: RunSpec, *, tmpfs_scratch_bytes: int) -> object:  # pragma: no cover
        raise AssertionError("the runner must not be called by _build_spec")
