"""Sandbox execution-service tests — the crash-safe, audited run lifecycle (#230).

Exercises :class:`app.sandbox.service.SandboxService` end-to-end against an in-memory
SQLite database, a **fake object store** (round-trips artifact bytes), and a **fake
runner** (returns a scripted :class:`RunResult` or raises), so the suite is offline
(no container engine, no MinIO). The isolation *wiring* is proven in
``test_sandbox_isolation.py``; this proves the service **around** the runner:

* AC-1: a run executes, captures stdout/stderr, and stores its produced file as a
  tenant-scoped artifact linked to the run;
* AC-2 (isolation-service negatives): a disabled tenant / over-quota run is refused
  BEFORE the runner is ever called (``status=denied``, audited); a wall-clock
  overrun maps to ``timeout``, an OOM to ``killed``;
* AC-3: two tenants' runs never see each other (a cross-tenant id → 404); each run
  starts from a fresh spec with no residue from a prior run;
* AC-4: the per-tenant concurrency + daily-runtime quotas are enforced; every run is
  audited (``code_run.started``/``.finished``/``.denied``, INV-6);
* crash safety: a runner crash writes ``failed``, never a stuck ``running``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    CodeRunRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    UserRepository,
)
from app.domain.entities import AuditEvent, CodeRunStatus, ResourceUsage, Role
from app.sandbox.service import SandboxReadService, SandboxService
from app.sandbox.spec import OutputFile, RunResult, RunSpec
from app.storage.keys import build_artifact_key
from tests._sandbox_helpers import sandbox_settings

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


# --- Fakes ------------------------------------------------------------------


class _FakeStore:
    """No-network stand-in for ``ObjectStore``'s artifact methods (#208 seam)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put_artifact(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> object:
        from app.storage.object_store import StoredObject

        key = build_artifact_key(tenant_id, data, filename)
        self.objects[(tenant_id, key)] = data
        return StoredObject(
            key=key, sha256=key.split("/")[2], size_bytes=len(data), content_type=content_type
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        return self.objects[(tenant_id, key)]


class _FakeRunner:
    """A scripted runner: records the spec it was handed, returns a result or raises."""

    def __init__(self, *, result: RunResult | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.specs: list[RunSpec] = []
        self.calls = 0

    async def run(self, spec: RunSpec, *, tmpfs_scratch_bytes: int) -> RunResult:
        self.calls += 1
        self.specs.append(spec)
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _ok_result(
    *, stdout: str = "ok", output_files: tuple[OutputFile, ...] = ()
) -> RunResult:
    return RunResult(
        status=CodeRunStatus.SUCCEEDED,
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration_ms=12,
        output_files=output_files,
        image_digest="sha256:deadbeef",
        resource_usage=ResourceUsage(peak_memory_bytes=1024, output_bytes=len(stdout)),
    )


# --- Fixtures ---------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


async def _make_tenant_user(
    session: AsyncSession, name: str, email: str
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await TenantRepository(session).create(name=name)
    user = await UserRepository(session, tenant.id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    return tenant.id, user.id


async def _enable_sandbox(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    enabled: bool = True,
    allowed_packages: tuple[str, ...] = (),
    egress_allowed: bool = False,
    egress_allowlist: tuple[str, ...] = (),
    max_runtime_s: int = 30,
    max_memory_mb: int = 512,
    daily_runtime_cap_s: int = 3600,
    max_concurrency: int = 2,
) -> None:
    """Seed a per-tenant sandbox policy row (#233) so the deny-by-default gate admits.

    Without a stored row code execution is DISABLED for the tenant (deny-by-default), so
    the happy-path service tests must first enable it. Values default to the config
    ceiling; override to prove a per-tenant setting flows through (egress/packages/quota).
    """
    await TenantSandboxPolicyRepository(session, tenant_id).upsert(
        enabled=enabled,
        allowed_packages=allowed_packages,
        denied_packages=(),
        egress_allowed=egress_allowed,
        egress_allowlist=egress_allowlist,
        max_runtime_s=max_runtime_s,
        max_memory_mb=max_memory_mb,
        daily_runtime_cap_s=daily_runtime_cap_s,
        max_concurrency=max_concurrency,
        updated_by=None,
    )


def _service(
    session: AsyncSession,
    store: _FakeStore,
    runner: _FakeRunner,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    settings_overrides: dict[str, object] | None = None,
) -> SandboxService:
    return SandboxService(
        tenant_id=tenant_id,
        owner_id=owner_id,
        runner=runner,  # type: ignore[arg-type]  # structural fake
        object_store=store,  # type: ignore[arg-type]  # structural fake
        settings=sandbox_settings(**(settings_overrides or {})),
        request_id="req-test",
        source_ip="203.0.113.9",
    )


async def _audit_actions(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    events: list[AuditEvent] = await AuditEventRepository(session, tenant_id).list_recent(limit=50)
    return [e.action for e in events]


# --- AC-1: run + capture stdout/stderr + produced file → artifact -----------


async def test_run_captures_output_and_stores_produced_file_as_artifact(
    session: AsyncSession,
) -> None:
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(session, tenant)
    store = _FakeStore()
    output = OutputFile(filename="result.csv", content_type="text/csv", data=b"a,b\n1,2\n")
    runner = _FakeRunner(result=_ok_result(stdout="done", output_files=(output,)))
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print('done')")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.SUCCEEDED
    persisted = await code_runs.get(run.id)
    assert persisted is not None
    assert persisted.status is CodeRunStatus.SUCCEEDED
    assert persisted.stdout == "done"
    assert persisted.exit_code == 0
    assert persisted.duration_ms == 12
    assert persisted.image_digest == "sha256:deadbeef"
    assert persisted.resource_usage is not None
    # The produced file became a tenant-scoped artifact linked to the run (CC-B).
    assert len(persisted.artifact_ids) == 1
    assert len(store.objects) == 1
    # Audited: started + finished (INV-6).
    actions = await _audit_actions(session, tenant)
    assert "code_run.started" in actions
    assert "code_run.finished" in actions
    # And an artifact.created for the produced file.
    assert "artifact.created" in actions


# --- AC-2: refused before execution when disabled / over quota --------------


async def test_disabled_tenant_run_is_denied_before_runner(session: AsyncSession) -> None:
    """The kill-switch: with the deploy-wide sandbox off, an enabled tenant policy is overridden.

    Even with a per-tenant policy that enables code execution, the deploy-wide
    ``SANDBOX_ENABLED`` kill-switch off (AND-ed in the effective policy, #233) denies the
    run before the runner — the kill-switch always wins.
    """
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(session, tenant)  # the tenant policy enables it...
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")

    service = _service(
        session, store, runner, tenant_id=tenant, owner_id=owner,
        # ...but the deploy-wide kill-switch is off → still denied.
        settings_overrides={"SANDBOX_ENABLED": "false"},
    )
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.DENIED
    assert runner.calls == 0  # the runner was NEVER called
    persisted = await code_runs.get(run.id)
    assert persisted is not None and persisted.status is CodeRunStatus.DENIED
    actions = await _audit_actions(session, tenant)
    assert "code_run.denied" in actions
    assert "code_run.started" not in actions


async def test_no_tenant_policy_run_is_denied_by_default(session: AsyncSession) -> None:
    """AC-3 (#233): with NO stored per-tenant policy, the run is denied (deny-by-default).

    The deploy-wide kill-switch is ON, but the tenant has never had a policy set — code
    execution stays DISABLED for the tenant until an admin enables it. The runner is
    never called; the refusal is audited.
    """
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    # Deliberately NO _enable_sandbox — the tenant has no policy row.
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.DENIED
    assert runner.calls == 0
    actions = await _audit_actions(session, tenant)
    assert "code_run.denied" in actions
    assert "code_run.started" not in actions


async def test_tenant_policy_disabled_run_is_denied(session: AsyncSession) -> None:
    """AC-1/AC-3 (#233): an admin who set enabled=false denies the run (before the runner)."""
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(session, tenant, enabled=False)  # admin explicitly OFF
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.DENIED
    assert runner.calls == 0


async def test_concurrency_quota_denies_over_the_cap(session: AsyncSession) -> None:
    """AC-4: over the per-tenant concurrency cap, a run is denied (before the runner)."""
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    # Enabled, concurrency 1 (clamped to the config ceiling of 1 below).
    await _enable_sandbox(session, tenant, max_concurrency=1)
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    # Cap of 1: create one already-running run + the new queued run → active count 2 > 1.
    running = await code_runs.create(owner_id=owner, code="a")
    await code_runs.mark_running(running.id, started_at=datetime.now(UTC))
    run = await code_runs.create(owner_id=owner, code="b")

    service = _service(
        session, store, runner, tenant_id=tenant, owner_id=owner,
        settings_overrides={"SANDBOX_MAX_CONCURRENT_PER_TENANT": "1"},
    )
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.DENIED
    assert runner.calls == 0
    actions = await _audit_actions(session, tenant)
    assert "code_run.denied" in actions


async def test_daily_runtime_quota_denies_when_exhausted(session: AsyncSession) -> None:
    """AC-4: over the per-tenant daily-runtime cap, a run is denied (before the runner)."""
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    # Enabled, daily cap 5s (clamped to the config ceiling of 5s below).
    await _enable_sandbox(session, tenant, daily_runtime_cap_s=5)
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    # A prior finished run that already consumed the whole daily budget.
    spent = await code_runs.create(owner_id=owner, code="a")
    await code_runs.mark_terminal(
        spent.id,
        status=CodeRunStatus.SUCCEEDED,
        finished_at=datetime.now(UTC),
        duration_ms=10_000,
    )
    run = await code_runs.create(owner_id=owner, code="b")

    service = _service(
        session, store, runner, tenant_id=tenant, owner_id=owner,
        settings_overrides={"SANDBOX_DAILY_RUNTIME_SECONDS_PER_TENANT": "5"},
    )
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.DENIED
    assert runner.calls == 0


@pytest.mark.parametrize(
    ("runner_status", "expected"),
    [
        (CodeRunStatus.TIMEOUT, CodeRunStatus.TIMEOUT),
        (CodeRunStatus.KILLED, CodeRunStatus.KILLED),
        (CodeRunStatus.FAILED, CodeRunStatus.FAILED),
    ],
)
async def test_terminal_status_from_runner_is_persisted(
    session: AsyncSession, runner_status: CodeRunStatus, expected: CodeRunStatus
) -> None:
    """AC-2: a wall-clock overrun → timeout, an OOM → killed, a non-zero exit → failed."""
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(session, tenant)
    store = _FakeStore()
    result = RunResult(
        status=runner_status, exit_code=137, stdout="", stderr="killed", duration_ms=30_000
    )
    runner = _FakeRunner(result=result)
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="while True: pass")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    status = await service.execute(session, run.id)

    assert status is expected
    persisted = await code_runs.get(run.id)
    assert persisted is not None and persisted.status is expected


# --- Crash safety: a runner crash → failed, never a stuck running -----------


async def test_runner_crash_writes_failed_not_stuck_running(session: AsyncSession) -> None:
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(session, tenant)
    store = _FakeStore()
    runner = _FakeRunner(raises=RuntimeError("runner exploded"))
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.FAILED
    persisted = await code_runs.get(run.id)
    assert persisted is not None
    assert persisted.status is CodeRunStatus.FAILED  # terminal, NOT stuck running
    assert persisted.finished_at is not None
    actions = await _audit_actions(session, tenant)
    assert "code_run.finished" in actions


# --- Idempotency: a non-queued run is not re-run ----------------------------


async def test_already_terminal_run_is_not_re_executed(session: AsyncSession) -> None:
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")
    await code_runs.mark_terminal(
        run.id, status=CodeRunStatus.SUCCEEDED, finished_at=datetime.now(UTC)
    )

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    status = await service.execute(session, run.id)

    assert status is CodeRunStatus.SUCCEEDED
    assert runner.calls == 0  # a duplicate delivery never re-runs


# --- AC-3: cross-tenant isolation + fresh per run ---------------------------


async def test_cross_tenant_code_run_id_is_404(session: AsyncSession) -> None:
    """INV-1: tenant B cannot read tenant A's run — the read service raises NotFound (→ 404)."""
    from app.core.errors import NotFoundError

    tenant_a, owner_a = await _make_tenant_user(session, "A", "a@x.test")
    tenant_b, owner_b = await _make_tenant_user(session, "B", "b@x.test")
    run = await CodeRunRepository(session, tenant_a).create(owner_id=owner_a, code="secret")

    reader_b = SandboxReadService(session, tenant_id=tenant_b, owner_id=owner_b)
    with pytest.raises(NotFoundError):
        await reader_b.get(run.id)

    # The owner in tenant A sees it.
    reader_a = SandboxReadService(session, tenant_id=tenant_a, owner_id=owner_a)
    got = await reader_a.get(run.id)
    assert got.id == run.id


async def test_non_owner_same_tenant_code_run_id_is_404(session: AsyncSession) -> None:
    """INV-2: a same-tenant non-owner sees a run as non-existent (404, not 403)."""
    from app.core.errors import NotFoundError

    tenant, owner = await _make_tenant_user(session, "Acme", "owner@x.test")
    other = await UserRepository(session, tenant).create(
        email="other@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    run = await CodeRunRepository(session, tenant).create(owner_id=owner, code="mine")

    reader = SandboxReadService(session, tenant_id=tenant, owner_id=other.id)
    with pytest.raises(NotFoundError):
        await reader.get(run.id)


async def test_each_run_gets_a_fresh_deny_by_default_spec(session: AsyncSession) -> None:
    """AC-3: two consecutive runs each get a fresh, network-none, no-mount spec (no residue).

    The spec the runner receives is rebuilt per run — same deny-by-default posture
    every time, carrying no state from the prior run (the container itself is
    ephemeral; the service never reuses a spec).
    """
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(session, tenant)
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    for _ in range(2):
        run = await code_runs.create(owner_id=owner, code="print(1)")
        await service.execute(session, run.id)

    assert runner.calls == 2
    for spec in runner.specs:
        assert spec.policy.egress.allowed is False
        assert spec.policy.egress.network_mode == "none"
        assert spec.policy.allow_package_install is False
        assert spec.inputs == ()  # no residue staged from a prior run


# --- #233: the per-tenant policy flows into the run spec + gate ---------------


def test_policy_service_metadata_ip_matches_the_sandbox_spec() -> None:
    """The policy service's local METADATA_IP must not drift from the sandbox spec's (G4).

    ``services/sandbox_policy_service`` duplicates the constant to avoid a services→
    sandbox import cycle; this pins the two definitions equal so the "never
    allowlistable" IP can never diverge between the governance write and the runner's
    egress policy.
    """
    from app.sandbox.spec import METADATA_IP as SPEC_METADATA_IP
    from app.services.sandbox_policy_service import METADATA_IP as SERVICE_METADATA_IP

    assert SERVICE_METADATA_IP == SPEC_METADATA_IP


async def test_tenant_egress_allowlist_flows_into_the_run_spec(session: AsyncSession) -> None:
    """AC-1/AC-2 (#233): an admin egress allowlist opens the run's egress to those targets."""
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(
        session, tenant, egress_allowed=True, egress_allowlist=("api.example.com:443",)
    )
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    await service.execute(session, run.id)

    assert runner.calls == 1
    spec = runner.specs[0]
    assert spec.policy.egress.allowed is True
    assert spec.policy.egress.allowlist == ("api.example.com:443",)
    assert spec.policy.egress.network_mode == "restricted"


async def test_metadata_ip_is_never_reachable_even_if_admin_lists_it(
    session: AsyncSession,
) -> None:
    """AC-2 (#233, G4): the cloud-metadata IP is stripped from egress — never reachable.

    Even if an admin lists the metadata IP in the egress allowlist, it is stripped both
    when stored (the policy service) AND when resolved for a run (the reader), so the run
    can never reach it. With only the metadata IP listed, egress collapses to none.
    """
    from app.sandbox.spec import METADATA_IP

    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    # Bypass the service's own stripping to prove the reader ALSO strips (defence in
    # depth): write the raw allowlist straight to the repository.
    await TenantSandboxPolicyRepository(session, tenant).upsert(
        enabled=True,
        allowed_packages=(),
        denied_packages=(),
        egress_allowed=True,
        egress_allowlist=(f"{METADATA_IP}:80",),
        max_runtime_s=30,
        max_memory_mb=512,
        daily_runtime_cap_s=3600,
        max_concurrency=2,
        updated_by=None,
    )
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    await service.execute(session, run.id)

    spec = runner.specs[0]
    # The metadata IP was stripped → the allowlist is empty → egress collapses to none.
    assert all(METADATA_IP not in t for t in spec.policy.egress.allowlist)
    assert spec.policy.egress.allowlist == ()
    assert spec.policy.egress.network_mode == "none"


async def test_tenant_runtime_is_clamped_to_the_config_ceiling(session: AsyncSession) -> None:
    """AC-2 (#233): a per-tenant runtime ABOVE the config ceiling is clamped down.

    An admin sets max_runtime_s = 9999, but the deploy-wide ceiling
    (``SANDBOX_WALL_CLOCK_SECONDS`` = 30) is the hard cap — the run spec carries the
    clamped 30, never the widened 9999 (a per-tenant value can only narrow).
    """
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    # The repo write is raw (no clamp); the reader clamps to the config ceiling.
    await TenantSandboxPolicyRepository(session, tenant).upsert(
        enabled=True,
        allowed_packages=(),
        denied_packages=(),
        egress_allowed=False,
        egress_allowlist=(),
        max_runtime_s=9999,  # far above the ceiling
        max_memory_mb=99_999,
        daily_runtime_cap_s=99_999,
        max_concurrency=999,
        updated_by=None,
    )
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="print(1)")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    await service.execute(session, run.id)

    spec = runner.specs[0]
    # Clamped to the config ceiling (30s wall-clock, 512 MiB) — never the widened value.
    assert spec.limits.wall_clock_seconds == 30
    assert spec.limits.memory_bytes == 512 * 1024 * 1024


async def test_tenant_packages_enable_install_in_the_spec(session: AsyncSession) -> None:
    """AC-1 (#233): an admin package allowlist enables package install for the run."""
    tenant, owner = await _make_tenant_user(session, "Acme", "a@acme.test")
    await _enable_sandbox(session, tenant, allowed_packages=("numpy",))
    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())
    code_runs = CodeRunRepository(session, tenant)
    run = await code_runs.create(owner_id=owner, code="import numpy")

    service = _service(session, store, runner, tenant_id=tenant, owner_id=owner)
    await service.execute(session, run.id)

    spec = runner.specs[0]
    assert spec.policy.allow_package_install is True


async def test_cross_tenant_policy_isolation(session: AsyncSession) -> None:
    """INV-1 (#233): tenant B's disabled policy does not admit a run for tenant A and vice versa.

    Tenant A enables the sandbox; tenant B never does. A run in tenant B is denied
    (deny-by-default) even though tenant A's policy is enabled — a policy is never
    consulted cross-tenant.
    """
    tenant_a, owner_a = await _make_tenant_user(session, "A", "a@x.test")
    tenant_b, owner_b = await _make_tenant_user(session, "B", "b@x.test")
    await _enable_sandbox(session, tenant_a)  # A enabled, B not

    store = _FakeStore()
    runner = _FakeRunner(result=_ok_result())

    # Tenant B's run is denied — B has no policy (A's enabled policy is not visible).
    run_b = await CodeRunRepository(session, tenant_b).create(owner_id=owner_b, code="print(1)")
    service_b = _service(session, store, runner, tenant_id=tenant_b, owner_id=owner_b)
    assert await service_b.execute(session, run_b.id) is CodeRunStatus.DENIED
    assert runner.calls == 0

    # Tenant A's run is admitted — its own enabled policy.
    run_a = await CodeRunRepository(session, tenant_a).create(owner_id=owner_a, code="print(1)")
    service_a = _service(session, store, runner, tenant_id=tenant_a, owner_id=owner_a)
    assert await service_a.execute(session, run_a.id) is CodeRunStatus.SUCCEEDED
    assert runner.calls == 1
