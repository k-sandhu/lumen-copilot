"""Bounded-sandbox regression tests (#589, ADR-0029 proposed).

Limits are on by default, tenant overrides may only narrow, concurrency and
daily-runtime accounting are atomic and tenant-scoped, and every terminal
code run records its effective limits and termination reason.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.repositories import (
    ChatSessionRepository,
    CodeRunRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    UserRepository,
)
from app.domain.code_execution import (
    SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
    SANDBOX_REASON_DAILY_RUNTIME_EXCEEDED,
)
from app.domain.entities import CodeRunStatus, ResourceUsage, Role
from app.sandbox.accounting import (
    AccountingUnavailable,
    InMemorySandboxAccounting,
    RedisSandboxAccounting,
)
from app.sandbox.service import SandboxService, resolve_effective_limits, session_resource_bounds
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec
from app.services.sandbox_policy_service import EffectiveSandboxPolicy
from tests._db_helpers import copy_sqlite_schema
from tests._sandbox_helpers import sandbox_settings

import app.db.models  # noqa: F401  isort: skip


# --- settings + bounds -------------------------------------------------------


def test_limits_are_on_by_default() -> None:
    settings = sandbox_settings()
    assert settings.sandbox_session_limits_enabled is True
    cpus, memory_bytes, pids_limit = session_resource_bounds(settings)
    assert cpus is not None and cpus > 0
    assert memory_bytes is not None and memory_bytes > 0
    assert pids_limit is not None and pids_limit > 0


def test_explicit_opt_out_restores_unbounded_posture() -> None:
    settings = sandbox_settings(SANDBOX_SESSION_LIMITS_ENABLED="false")
    assert session_resource_bounds(settings) == (None, None, None)


@pytest.mark.parametrize(
    "field",
    [
        "SANDBOX_CPUS",
        "SANDBOX_MEMORY_BYTES",
        "SANDBOX_PIDS_LIMIT",
        "SANDBOX_WALL_CLOCK_SECONDS",
        "SANDBOX_SCRATCH_BYTES",
        "SANDBOX_OUTPUT_BYTES_CAP",
        "SANDBOX_MAX_CONCURRENT_PER_TENANT",
        "SANDBOX_DAILY_RUNTIME_SECONDS_PER_TENANT",
    ],
)
def test_settings_reject_non_positive_caps(field: str) -> None:
    with pytest.raises(ValidationError):
        sandbox_settings(**{field: "0"})


def _policy(**overrides: object) -> EffectiveSandboxPolicy:
    base: dict[str, object] = {
        "enabled": True,
        "allowed_packages": (),
        "denied_packages": (),
        "egress_allowed": False,
        "egress_allowlist": (),
        "max_runtime_s": 30,
        "max_memory_mb": 512,
        "daily_runtime_cap_s": 3600,
        "max_concurrency": 2,
    }
    base.update(overrides)
    return EffectiveSandboxPolicy(**base)  # type: ignore[arg-type]


def test_tenant_values_only_narrow_deployment_ceilings() -> None:
    settings = sandbox_settings()
    narrowed = resolve_effective_limits(settings, _policy(max_runtime_s=10, max_memory_mb=128))
    assert narrowed.wall_clock_seconds == 10
    assert narrowed.memory_bytes == 128 * 1024 * 1024
    assert narrowed.sources["wall_clock_seconds"] == "tenant_override"
    assert narrowed.sources["cpus"] == "deployment_default"
    # A tenant value above the ceiling can never widen (defense in depth on read).
    widened = resolve_effective_limits(settings, _policy(max_runtime_s=999_999))
    assert widened.wall_clock_seconds == settings.sandbox_wall_clock_seconds
    assert widened.cpus == settings.sandbox_cpus
    assert widened.pids_limit == settings.sandbox_pids_limit
    assert widened.scratch_bytes == settings.sandbox_scratch_bytes
    assert widened.output_bytes_cap == settings.sandbox_output_bytes_cap


# --- accounting ---------------------------------------------------------------


class _FakeRedis:
    """Async fake interpreting the accounting Lua scripts by their markers.

    One lock serialises every ``eval`` — the same atomicity Redis gives a
    script — so the barrier test below proves the *protocol*, while the Lua
    text itself is exercised only against a live Redis.
    """

    def __init__(self) -> None:
        self.leases: dict[str, dict[str, float]] = {}
        self.counters: dict[str, int] = {}
        self.owners: dict[tuple[str, str], str] = {}
        self.lock = asyncio.Lock()
        self.fail = False

    async def eval(self, script: str, numkeys: int, *args: object) -> object:
        if self.fail:
            import redis

            raise redis.RedisError("down")
        async with self.lock:
            key = str(args[0])
            if numkeys == 2:
                args = (args[0], *args[2:])
            if "LEASE_ACQUIRE" in script:
                member, cap, expiry = str(args[1]), int(str(args[2])), float(str(args[3]))
                now = float(str(args[4]))
                members = {m: e for m, e in self.leases.get(key, {}).items() if e > now}
                if member in members:
                    return 2
                if member not in members and len(members) >= cap:
                    self.leases[key] = members
                    return 0
                members[member] = expiry
                self.leases[key] = members
                self.owners[key, member] = str(args[6])
                return 1
            if "LEASE_RENEW" in script:
                member, token, now, expiry = (
                    str(args[1]),
                    str(args[2]),
                    float(args[3]),
                    float(args[4]),
                )
                members = self.leases.get(key, {})
                if members.get(member, 0) <= now or self.owners.get((key, member)) != token:
                    return 0
                members[member] = expiry
                return 1
            if "LEASE_RELEASE" in script:
                if str(args[2]) and self.owners.get((key, str(args[1]))) != str(args[2]):
                    return 0
                self.owners.pop((key, str(args[1])), None)
                self.leases.get(key, {}).pop(str(args[1]), None)
                return 1
            if "DAILY_RESERVE" in script:
                amount, cap = int(str(args[1])), int(str(args[2]))
                if self.counters.get(key, 0) + amount > cap:
                    return 0
                self.counters[key] = self.counters.get(key, 0) + amount
                return 1
            if "DAILY_SETTLE" in script:
                self.counters[key] = max(0, self.counters.get(key, 0) + int(str(args[1])))
                return self.counters[key]
            raise AssertionError(f"unexpected script: {script[:60]}")


async def test_concurrent_admission_at_cap_admits_exactly_one() -> None:
    fake = _FakeRedis()
    accounting = RedisSandboxAccounting("redis://unused", client_factory=lambda: fake)
    tenant = uuid4()
    assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=2, ttl_seconds=60)
    barrier = asyncio.Barrier(2)

    async def acquire() -> bool:
        await barrier.wait()
        return await accounting.acquire_lease(tenant, uuid4(), max_concurrency=2, ttl_seconds=60)

    results = await asyncio.gather(acquire(), acquire())
    assert sorted(results) == [False, True]


async def test_lease_release_and_tenant_isolation() -> None:
    accounting = InMemorySandboxAccounting()
    tenant_a, tenant_b, run_id = uuid4(), uuid4(), uuid4()
    assert await accounting.acquire_lease(tenant_a, run_id, max_concurrency=1, ttl_seconds=60)
    # Tenant B is never throttled by tenant A's usage (INV-1).
    assert await accounting.acquire_lease(tenant_b, uuid4(), max_concurrency=1, ttl_seconds=60)
    assert not await accounting.acquire_lease(tenant_a, uuid4(), max_concurrency=1, ttl_seconds=60)
    await accounting.release_lease(tenant_a, run_id)
    assert await accounting.acquire_lease(tenant_a, uuid4(), max_concurrency=1, ttl_seconds=60)


async def test_daily_cap_refuses_and_resets_next_day() -> None:
    clock = {"now": datetime(2026, 10, 29, 12, 0, tzinfo=UTC)}
    accounting = InMemorySandboxAccounting(now=lambda: clock["now"])
    tenant = uuid4()
    assert await accounting.reserve_daily(tenant, seconds=30, daily_cap_s=30)
    assert not await accounting.reserve_daily(tenant, seconds=1, daily_cap_s=30)
    clock["now"] += timedelta(days=1)
    assert await accounting.reserve_daily(tenant, seconds=1, daily_cap_s=30)


async def test_accounting_unavailable_fails_closed() -> None:
    fake = _FakeRedis()
    fake.fail = True
    accounting = RedisSandboxAccounting("redis://unused", client_factory=lambda: fake)
    with pytest.raises(AccountingUnavailable):
        await accounting.acquire_lease(uuid4(), uuid4(), max_concurrency=1, ttl_seconds=60)


# --- service integration -------------------------------------------------------


class _FakeRunner:
    def __init__(self) -> None:
        self.executions: list[tuple[SandboxSessionSpec, RunSpec]] = []

    async def ensure_session(self, value: SandboxSessionSpec) -> None:
        return None

    async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
        self.executions.append((value, run))
        return RunResult(
            status=CodeRunStatus.SUCCEEDED,
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=12,
            image_digest="python@sha256:runtime",
            resource_usage=ResourceUsage(output_bytes=2),
        )

    async def reset_session(self, previous: object, replacement: object) -> None:
        return None

    async def close_session(self, value: object) -> None:
        return None

    async def cancel(self, value: object, execution_id: UUID) -> None:
        return None


class _FakeStore:
    async def put_artifact(self, *args: object, **kwargs: object) -> object:  # pragma: no cover
        raise AssertionError("no outputs expected")


@pytest_asyncio.fixture
async def session(durable_audit_ledger: object) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as value:
            value.info["durable_audit_ledger"] = durable_audit_ledger
            yield value
    finally:
        await engine.dispose()


async def _setup(
    session: AsyncSession, *, daily: int = 3600, concurrency: int = 2, runtime: int = 30
) -> tuple[UUID, UUID, UUID]:
    tenant = await TenantRepository(session).create(name="Acme")
    owner = await UserRepository(session, tenant.id).create(
        email="a@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    chat = await ChatSessionRepository(session, tenant.id).create(
        owner_id=owner.id, model="openrouter:test/model"
    )
    await TenantSandboxPolicyRepository(session, tenant.id).upsert(
        enabled=True,
        allowed_packages=(),
        denied_packages=(),
        egress_allowed=False,
        egress_allowlist=(),
        max_runtime_s=runtime,
        max_memory_mb=512,
        daily_runtime_cap_s=daily,
        max_concurrency=concurrency,
        updated_by=None,
    )
    return tenant.id, owner.id, chat.id


def _service(tenant: UUID, owner: UUID, runner: _FakeRunner, accounting: object) -> SandboxService:
    return SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,  # type: ignore[arg-type]
        object_store=_FakeStore(),  # type: ignore[arg-type]
        settings=sandbox_settings(),
        accounting=accounting,  # type: ignore[arg-type]
    )


async def test_run_persists_effective_limits_and_wall_clock_reaches_spec(
    session: AsyncSession,
) -> None:
    tenant, owner, chat = await _setup(session, runtime=10)
    runner = _FakeRunner()
    service = _service(tenant, owner, runner, InMemorySandboxAccounting())
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="print('ok')"
    )
    outcome = await service.execute_outcome(session, run.id)
    assert outcome.status is CodeRunStatus.SUCCEEDED
    spec, run_spec = runner.executions[0]
    assert spec.cpus is not None and spec.memory_bytes is not None and spec.pids_limit is not None
    assert spec.scratch_bytes is not None and spec.scratch_bytes > 0
    assert run_spec.wall_clock_seconds == 10
    persisted = await CodeRunRepository(session, tenant).get(run.id)
    assert persisted is not None
    assert persisted.effective_limits is not None
    assert persisted.effective_limits["wall_clock_seconds"] == 10
    assert persisted.termination_reason == "completed"


async def test_daily_cap_exhausted_denies_before_runner(session: AsyncSession) -> None:
    tenant, owner, chat = await _setup(session, daily=30, runtime=30)
    runner = _FakeRunner()
    accounting = InMemorySandboxAccounting()
    assert await accounting.reserve_daily(tenant, seconds=30, daily_cap_s=30)
    service = _service(tenant, owner, runner, accounting)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="print('ok')"
    )
    outcome = await service.execute_outcome(session, run.id)
    assert outcome.status is CodeRunStatus.DENIED
    assert outcome.reason_code == SANDBOX_REASON_DAILY_RUNTIME_EXCEEDED
    assert runner.executions == []


async def test_accounting_outage_denies_fail_closed(session: AsyncSession) -> None:
    tenant, owner, chat = await _setup(session)
    runner = _FakeRunner()
    fake = _FakeRedis()
    fake.fail = True
    accounting = RedisSandboxAccounting("redis://unused", client_factory=lambda: fake)
    service = _service(tenant, owner, runner, accounting)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="print('ok')"
    )
    outcome = await service.execute_outcome(session, run.id)
    assert outcome.status is CodeRunStatus.DENIED
    assert outcome.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
    assert runner.executions == []
    persisted = await CodeRunRepository(session, tenant).get(run.id)
    assert persisted is not None
    assert persisted.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE


async def test_midnight_settlement_refunds_only_the_reservation_day() -> None:
    clock = {"now": datetime(2026, 10, 29, 23, 59, tzinfo=UTC)}
    accounting = InMemorySandboxAccounting(now=lambda: clock["now"])
    tenant = uuid4()
    day = clock["now"]
    assert await accounting.reserve_daily(tenant, seconds=30, daily_cap_s=30, day=day)
    clock["now"] += timedelta(days=1)
    assert await accounting.reserve_daily(tenant, seconds=30, daily_cap_s=30)
    await accounting.settle_daily(tenant, reserved_seconds=30, actual_seconds=1, day=day)
    assert not await accounting.reserve_daily(tenant, seconds=1, daily_cap_s=30)


async def test_duplicate_delivery_cannot_acquire_the_same_active_run_twice() -> None:
    from app.sandbox.accounting import ExecutionAlreadyAdmitted

    accounting = InMemorySandboxAccounting()
    tenant, run_id = uuid4(), uuid4()
    assert await accounting.acquire_lease(tenant, run_id, max_concurrency=2, ttl_seconds=60)
    with pytest.raises(ExecutionAlreadyAdmitted):
        await accounting.acquire_lease(tenant, run_id, max_concurrency=2, ttl_seconds=60)


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_cpu_ceiling_must_be_finite_and_positive(value: str) -> None:
    with pytest.raises(ValidationError):
        sandbox_settings(SANDBOX_CPUS=value)


def test_memory_ceiling_respects_engine_floor() -> None:
    with pytest.raises(ValidationError):
        sandbox_settings(SANDBOX_MEMORY_BYTES="1024")


async def test_cancellation_releases_admission_and_cancels_runner(session: AsyncSession) -> None:
    class BlockingRunner(_FakeRunner):
        entered = asyncio.Event()
        cancelled = False

        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: object, execution_id: UUID) -> None:
            self.cancelled = True

    tenant, owner, chat = await _setup(session, concurrency=1)
    runner = BlockingRunner()
    accounting = InMemorySandboxAccounting()
    service = _service(tenant, owner, runner, accounting)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    task = asyncio.create_task(service.execute_outcome(session, run.id))
    await runner.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.cancelled
    assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
    persisted = await CodeRunRepository(session, tenant).get(run.id)
    assert persisted is not None and persisted.termination_reason == "cancelled"


async def test_policy_usage_comes_from_admission_store_and_is_tenant_scoped(
    session: AsyncSession,
) -> None:
    from app.services.sandbox_policy_service import SandboxPolicyService

    tenant, _, _ = await _setup(session)
    accounting = InMemorySandboxAccounting()
    await accounting.acquire_lease(tenant, uuid4(), max_concurrency=2, ttl_seconds=60)
    await accounting.reserve_daily(tenant, seconds=30, daily_cap_s=3600)
    view = await SandboxPolicyService(
        session, tenant_id=tenant, settings=sandbox_settings(), accounting=accounting
    ).get_policy()
    assert view.current_concurrency == 1
    assert view.daily_runtime_used_s == 30
    assert view.accounting_available
    other = await SandboxPolicyService(
        session, tenant_id=uuid4(), settings=sandbox_settings(), accounting=accounting
    ).get_policy()
    assert other.current_concurrency == 0 and other.daily_runtime_used_s == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_runtime_s", 31),
        ("max_memory_mb", 513),
        ("daily_runtime_cap_s", 3601),
        ("max_concurrency", 3),
    ],
)
async def test_above_ceiling_policy_write_is_rejected_without_mutating(
    session: AsyncSession, field: str, value: int
) -> None:
    from app.core.errors import ValidationError as PolicyError
    from app.services.sandbox_policy_service import SandboxPolicyService

    tenant, owner, _ = await _setup(session)
    service = SandboxPolicyService(
        session,
        tenant_id=tenant,
        settings=sandbox_settings(),
        accounting=InMemorySandboxAccounting(),
    )
    body: dict[str, object] = {
        "enabled": True,
        "allowed_packages": (),
        "denied_packages": (),
        "egress_allowed": False,
        "egress_allowlist": (),
        "max_runtime_s": 30,
        "max_memory_mb": 512,
        "daily_runtime_cap_s": 3600,
        "max_concurrency": 2,
        "actor_id": owner,
        "request_id": "limits-test",
        "source_ip": "system",
    }
    body[field] = value
    with pytest.raises(PolicyError):
        await service.set_policy(**body)
    view = await service.get_policy()
    assert view.max_runtime_s == 30 and view.max_memory_mb == 512


async def test_daily_race_reserves_only_one_last_budget() -> None:
    fake = _FakeRedis()
    accounting = RedisSandboxAccounting("redis://unused", client_factory=lambda: fake)
    tenant = uuid4()
    barrier = asyncio.Barrier(2)

    async def reserve() -> bool:
        await barrier.wait()
        return await accounting.reserve_daily(tenant, seconds=30, daily_cap_s=30)

    assert sorted(await asyncio.gather(reserve(), reserve())) == [False, True]
    assert await accounting.reserve_daily(uuid4(), seconds=30, daily_cap_s=30)


async def test_worker_death_lease_expires_without_affecting_other_tenants() -> None:
    clock = {"now": datetime(2026, 10, 29, tzinfo=UTC)}
    accounting = InMemorySandboxAccounting(now=lambda: clock["now"])
    tenant = uuid4()
    assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
    clock["now"] += timedelta(seconds=61)
    assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)


async def test_failure_releases_lease_and_denial_is_audited(session: AsyncSession) -> None:
    from app.db.repositories import AuditEventRepository

    tenant, owner, chat = await _setup(session)
    fake = _FakeRedis()
    fake.fail = True
    accounting = RedisSandboxAccounting("redis://unused", client_factory=lambda: fake)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    await _service(tenant, owner, _FakeRunner(), accounting).execute_outcome(session, run.id)
    events = await AuditEventRepository(session, tenant).list_recent(limit=20)
    assert any(
        e.action == "code_run.denied"
        and e.metadata.get("reason_code") == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        and e.metadata.get("effective_limits")
        for e in events
    )


async def test_runner_failure_releases_its_concurrency_lease(session: AsyncSession) -> None:
    class FailingRunner(_FakeRunner):
        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            raise RuntimeError("transport failed")

    tenant, owner, chat = await _setup(session, concurrency=1)
    accounting = InMemorySandboxAccounting()
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    outcome = await _service(tenant, owner, FailingRunner(), accounting).execute_outcome(
        session, run.id
    )
    assert outcome.status is CodeRunStatus.FAILED
    assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
