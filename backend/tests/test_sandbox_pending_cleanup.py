"""TF1: cancellation needs confirmation; live cleanup retains fenced admission."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models
from app.db.repositories import CodeRunRepository
from app.domain.code_execution import SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
from app.domain.entities import CodeRunStatus, ResourceUsage
from app.sandbox.accounting import AccountingUnavailable, InMemorySandboxAccounting
from app.sandbox.runner import SandboxRunnerUnavailable
from app.sandbox.service import SandboxService
from app.sandbox.spec import OutputFile, RunResult, RunSpec, SandboxSessionSpec
from app.storage.keys import build_artifact_key
from tests._sandbox_helpers import sandbox_settings
from tests.test_sandbox_limits import _FakeRunner, _FakeStore, _setup
from tests.test_sandbox_limits import session as session


@pytest.fixture(autouse=True)
def admission_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the service's reservation day aligned with the injected accountant."""

    class AdmissionDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 10, 30, 12, tzinfo=UTC)

    monkeypatch.setattr("app.sandbox.service.datetime", AdmissionDateTime)


class BarrierSleep:
    def __init__(self) -> None:
        self.waits: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()

    async def __call__(self, _delay: float) -> None:
        release = asyncio.get_running_loop().create_future()
        self.waits.put_nowait(release)
        await release


class CountingAccounting(InMemorySandboxAccounting):
    settlements = 0
    releases = 0

    async def settle_daily(self, *args: Any, **kwargs: Any) -> None:
        await super().settle_daily(*args, **kwargs)
        self.settlements += 1

    async def release_lease(self, *args: Any, **kwargs: Any) -> None:
        await super().release_lease(*args, **kwargs)
        self.releases += 1


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("renewal_failure", [False, True])
async def test_unconfirmed_kill_retries_same_identity_without_refund(
    session: AsyncSession,  # noqa: F811 - shared fixture
    monkeypatch: pytest.MonkeyPatch,
    background: bool,
    interrupted: bool,
    renewal_failure: bool,
) -> None:
    from app.tasks import run_sandbox

    entered, kill_entered = asyncio.Event(), asyncio.Event()
    retry, heartbeat = BarrierSleep(), BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]

    class Accounting(CountingAccounting):
        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            if renewal_failure:
                raise AccountingUnavailable("secret http://private.invalid/accounting")
            await super().renew_lease(*args, **kwargs)

    accounting = Accounting(now=lambda: clock[0])

    class Runner(_FakeRunner):
        remote_running = True
        kills: list[tuple[SandboxSessionSpec, UUID]] = []

        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            self.executions.append((value, run))
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: UUID) -> None:
            self.kills.append((value, execution_id))
            if len(self.kills) == 1:
                kill_entered.set()
                if interrupted:
                    await asyncio.Event().wait()
                raise SandboxRunnerUnavailable("secret http://private.invalid/kill")
            self.remote_running = False

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    runner = Runner()
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        object_store=_FakeStore(),
        settings=sandbox_settings(),
        accounting=accounting,
    )
    # Assign the injected wait seams before dispatch (ignored by the unfixed code).
    service._retry_sleep = retry
    service._lease_sleep = heartbeat

    @asynccontextmanager
    async def scope(_tenant: UUID) -> AsyncIterator[AsyncSession]:
        yield session

    if background:
        monkeypatch.setattr(run_sandbox, "tenant_session_scope", scope)
        monkeypatch.setattr(run_sandbox, "HttpSandboxRunner", lambda *a, **kw: runner)
        monkeypatch.setattr(run_sandbox, "ObjectStore", lambda *a: _FakeStore())
        monkeypatch.setattr(run_sandbox, "SandboxService", lambda **kw: service)
        work = run_sandbox.execute_code_run_async(run.id, tenant, settings=sandbox_settings())
    else:
        work = service.execute_outcome(session, run.id)
    task = asyncio.create_task(work)
    pending_wait: asyncio.Task[Any] | None = None
    try:
        await entered.wait()
        if renewal_failure:
            (await heartbeat.waits.get()).set_result(None)
        else:
            task.cancel()
        await kill_entered.wait()
        if interrupted:
            task.cancel()
        pending_wait = asyncio.create_task(retry.waits.get())
        done, _ = await asyncio.wait({task, pending_wait}, return_when=asyncio.FIRST_COMPLETED)
        assert pending_wait in done, "Unconfirmed kill must remain producer-owned pending cleanup"
        release = pending_wait.result()
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.RUNNING
        events = list(
            await session.scalars(
                select(models.AuditEvent).where(models.AuditEvent.action == "code_run.cancelled")
            )
        )
        assert len(events) == 1 and events[0].outcome == "error"
        assert events[0].event_metadata["reason_code"] in {
            "sandbox_runner_unavailable",
            "sandbox_cancel_interrupted",
        }
        assert "private.invalid" not in str(events[0].event_metadata)
        assert await accounting.usage(tenant) == (1, 30)
        assert accounting.settlements == accounting.releases == 0
        # Renew through deterministic ticks, including a parked retry wait.
        for _ in range(4):
            tick = await heartbeat.waits.get()
            clock[0] += timedelta(seconds=1 if renewal_failure else 100)
            tick.set_result(None)
        tick = await heartbeat.waits.get()  # prior renewal completed
        assert not await accounting.acquire_lease(
            tenant, uuid4(), max_concurrency=1, ttl_seconds=60
        )
        assert runner.remote_running
        release.set_result(None)
        # R3-001: a pure accounting loss returns a killed outcome instead of
        # propagating CancelledError. An interrupted kill means the producer
        # itself was cancelled during cleanup, so that cancellation still wins.
        accounting_return = renewal_failure and not interrupted
        if accounting_return:
            outcome = await task
            if background:
                assert outcome is CodeRunStatus.KILLED
            else:
                assert outcome.status is CodeRunStatus.KILLED
                assert outcome.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not runner.remote_running
        assert len(runner.executions) == 1 and runner.kills == [runner.kills[0]] * 2
        assert runner.kills[0][1] == run.id
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.KILLED
        assert stored.termination_reason == (
            SANDBOX_REASON_ACCOUNTING_UNAVAILABLE if renewal_failure else "cancelled"
        )
        assert accounting.settlements == accounting.releases == 1
        assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
        events = list(
            await session.scalars(
                select(models.AuditEvent).where(
                    models.AuditEvent.action == "code_run.cancelled",
                    models.AuditEvent.outcome == "allowed",
                )
            )
        )
        if renewal_failure:
            # Accounting kills are not user Stops: no allowed cancellation.
            assert events == []
            finished = list(
                await session.scalars(
                    select(models.AuditEvent).where(
                        models.AuditEvent.action == "code_run.finished",
                        models.AuditEvent.outcome == "error",
                    )
                )
            )
            assert len(finished) == 1
            assert finished[0].event_metadata["reason_code"] == (
                SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
            )
        else:
            assert len(events) == 1
        assert tick.cancelled()  # producer stopped its heartbeat
    finally:
        if pending_wait is not None:
            pending_wait.cancel()
        task.cancel()
        await asyncio.gather(
            task, *([pending_wait] if pending_wait else []), return_exceptions=True
        )


async def test_dead_producer_expires_without_refunding_pending_execution(
    session: AsyncSession,  # noqa: F811 - shared fixture
) -> None:
    entered = asyncio.Event()
    retry, heartbeat = BarrierSleep(), BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]
    accounting = CountingAccounting(now=lambda: clock[0])

    class Runner(_FakeRunner):
        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: object, execution_id: UUID) -> None:
            raise SandboxRunnerUnavailable("unconfirmed")

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=Runner(),
        object_store=_FakeStore(),
        settings=sandbox_settings(),
        accounting=accounting,
    )
    service._retry_sleep, service._lease_sleep = retry, heartbeat
    task = asyncio.create_task(service.execute_outcome(session, run.id))
    await entered.wait()
    task.cancel()
    waiter = asyncio.create_task(retry.waits.get())
    done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    try:
        assert waiter in done
        heartbeat_wait = await heartbeat.waits.get()
        task.cancel()  # producer shutdown while parked: no detached heartbeat
        await asyncio.gather(task, return_exceptions=True)
        assert heartbeat_wait.cancelled()
        assert accounting.settlements == accounting.releases == 0
        clock[0] += timedelta(seconds=331)
        assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
        assert (await accounting.usage(tenant))[1] == 30
    finally:
        waiter.cancel()
        task.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)


@pytest.mark.parametrize("redis", [False, True])
async def test_renewal_fences_expired_and_replaced_owners(redis: bool) -> None:
    from app.sandbox.accounting import AccountingUnavailable, RedisSandboxAccounting
    from tests.test_sandbox_limits import _FakeRedis

    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]
    fake = _FakeRedis()
    accounting = (
        RedisSandboxAccounting("redis://unused", client_factory=lambda: fake, now=lambda: clock[0])
        if redis
        else InMemorySandboxAccounting(now=lambda: clock[0])
    )
    tenant, run, old, new = uuid4(), uuid4(), uuid4(), uuid4()
    assert await accounting.acquire_lease(
        tenant, run, max_concurrency=1, ttl_seconds=60, owner_token=old
    )
    clock[0] += timedelta(seconds=40)
    await accounting.renew_lease(tenant, run, owner_token=old, ttl_seconds=60)
    clock[0] += timedelta(seconds=40)
    assert not await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
    clock[0] += timedelta(seconds=61)
    with pytest.raises(AccountingUnavailable):
        await accounting.renew_lease(tenant, run, owner_token=old, ttl_seconds=60)
    assert await accounting.acquire_lease(
        tenant, run, max_concurrency=1, ttl_seconds=60, owner_token=new
    )
    with pytest.raises(AccountingUnavailable):
        await accounting.renew_lease(tenant, run, owner_token=old, ttl_seconds=60)
    await accounting.release_lease(tenant, run, owner_token=old)
    assert not await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
    await accounting.release_lease(tenant, run, owner_token=new)
    assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)


class RecordingSleep:
    """Retry wait that records requested delays and returns immediately."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def test_renewal_failure_after_execution_finalises_normally(
    session: AsyncSession,  # noqa: F811 - shared fixture
) -> None:
    """R3-001: accounting loss after ``execute`` returns must not cancel finalisation."""
    heartbeat = BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]

    class Accounting(CountingAccounting):
        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            raise AccountingUnavailable("secret http://private.invalid/accounting")

    accounting = Accounting(now=lambda: clock[0])

    class Runner(_FakeRunner):
        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            self.executions.append((value, run))
            return RunResult(
                status=CodeRunStatus.SUCCEEDED,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_ms=12,
                output_files=(
                    OutputFile(filename="chart.png", content_type="image/png", data=b"\x89PNG"),
                ),
                image_digest="python@sha256:runtime",
                resource_usage=ResourceUsage(output_bytes=2),
            )

    class ParkingStore:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release: asyncio.Future[None] | None = None

        async def put_artifact(
            self, tenant_id: str, data: bytes, content_type: str, filename: str
        ) -> object:
            from app.storage.object_store import StoredObject

            self.release = asyncio.get_running_loop().create_future()
            self.entered.set()
            await self.release
            key = build_artifact_key(tenant_id, data, filename)
            return StoredObject(
                key=key, sha256=key.split("/")[2], size_bytes=len(data), content_type=content_type
            )

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    store = ParkingStore()
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=Runner(),
        object_store=store,  # type: ignore[arg-type]
        settings=sandbox_settings(),
        accounting=accounting,
    )
    service._retry_sleep = RecordingSleep()  # type: ignore[assignment]
    service._lease_sleep = heartbeat
    task = asyncio.create_task(service.execute_outcome(session, run.id))
    try:
        await store.entered.wait()
        (await heartbeat.waits.get()).set_result(None)
        # Next heartbeat wait proves the failed-renewal tick was fully handled.
        await heartbeat.waits.get()
        assert store.release is not None
        if not store.release.done():
            store.release.set_result(None)
        outcome = await task
        assert outcome.status is CodeRunStatus.SUCCEEDED
        assert outcome.reason_code is None
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.SUCCEEDED
        assert stored.termination_reason == "completed"
        finished = list(
            await session.scalars(
                select(models.AuditEvent).where(models.AuditEvent.action == "code_run.finished")
            )
        )
        assert len(finished) == 1
        assert accounting.settlements == accounting.releases == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_pending_cleanup_backs_off_and_audits_first_failure_only(
    session: AsyncSession,  # noqa: F811 - shared fixture
) -> None:
    """R3-002: repeated kill failures back off and do not flood the audit trail."""
    entered = asyncio.Event()
    retry, heartbeat = RecordingSleep(), BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]
    accounting = CountingAccounting(now=lambda: clock[0])

    class Runner(_FakeRunner):
        kills = 0

        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: object, execution_id: UUID) -> None:
            self.kills += 1
            if self.kills <= 8:
                raise SandboxRunnerUnavailable("unconfirmed")

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    runner = Runner()
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        object_store=_FakeStore(),
        settings=sandbox_settings(),
        accounting=accounting,
    )
    service._retry_sleep, service._lease_sleep = retry, heartbeat  # type: ignore[assignment]
    task = asyncio.create_task(service.execute_outcome(session, run.id))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.kills == 9
        assert retry.delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
        errors = list(
            await session.scalars(
                select(models.AuditEvent).where(
                    models.AuditEvent.action == "code_run.cancelled",
                    models.AuditEvent.outcome == "error",
                )
            )
        )
        assert len(errors) == 1
        allowed = list(
            await session.scalars(
                select(models.AuditEvent).where(
                    models.AuditEvent.action == "code_run.cancelled",
                    models.AuditEvent.outcome == "allowed",
                )
            )
        )
        assert len(allowed) == 1
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.KILLED
        assert stored.termination_reason == "cancelled"
        assert accounting.settlements == accounting.releases == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _failing_accounting(clock: list[datetime]) -> CountingAccounting:
    class Accounting(CountingAccounting):
        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            raise AccountingUnavailable("secret http://private.invalid/accounting")

    return Accounting(now=lambda: clock[0])


async def _fail_one_heartbeat_tick(heartbeat: BarrierSleep) -> None:
    """Release one tick; the next parked wait proves the failure was handled."""
    (await heartbeat.waits.get()).set_result(None)
    await heartbeat.waits.get()


async def test_renewal_failure_during_ensure_session_denies_without_execute(
    session: AsyncSession,  # noqa: F811 - shared fixture
) -> None:
    """R4-001/MB: accounting loss during ``ensure_session`` -> denied, no execute."""
    heartbeat = BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]
    accounting = _failing_accounting(clock)
    ensure_entered, ensure_release = asyncio.Event(), asyncio.Event()

    class Runner(_FakeRunner):
        kills = 0

        async def ensure_session(self, value: SandboxSessionSpec) -> None:
            ensure_entered.set()
            await ensure_release.wait()

        async def cancel(self, value: object, execution_id: UUID) -> None:
            self.kills += 1

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    runner = Runner()
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        object_store=_FakeStore(),
        settings=sandbox_settings(),
        accounting=accounting,
    )
    service._retry_sleep, service._lease_sleep = RecordingSleep(), heartbeat  # type: ignore[assignment]
    task = asyncio.create_task(service.execute_outcome(session, run.id))
    try:
        await ensure_entered.wait()
        await _fail_one_heartbeat_tick(heartbeat)
        ensure_release.set()
        outcome = await task
        assert outcome.status is CodeRunStatus.DENIED
        assert outcome.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        assert runner.executions == []
        assert runner.kills == 0
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.DENIED
        assert stored.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        assert accounting.settlements == accounting.releases == 1
    finally:
        ensure_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_renewal_failure_during_mark_running_never_runs_execute_body(
    session: AsyncSession,  # noqa: F811 - shared fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4-001/MC: accounting loss during ``mark_running`` -> execute body never runs."""
    heartbeat = BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]
    accounting = _failing_accounting(clock)
    running_entered, running_release = asyncio.Event(), asyncio.Event()
    execute_body_ran = False
    original_mark_running = CodeRunRepository.mark_running

    async def parked_mark_running(self: CodeRunRepository, *args: Any, **kwargs: Any) -> Any:
        running_entered.set()
        await running_release.wait()
        return await original_mark_running(self, *args, **kwargs)

    monkeypatch.setattr(CodeRunRepository, "mark_running", parked_mark_running)

    class Runner(_FakeRunner):
        kills = 0

        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            nonlocal execute_body_ran
            execute_body_ran = True
            self.executions.append((value, run))
            return RunResult(
                status=CodeRunStatus.SUCCEEDED,
                exit_code=0,
                stdout="ok",
                stderr="",
                duration_ms=1,
                image_digest="python@sha256:runtime",
                resource_usage=ResourceUsage(output_bytes=2),
            )

        async def cancel(self, value: object, execution_id: UUID) -> None:
            self.kills += 1

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    runner = Runner()
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        object_store=_FakeStore(),
        settings=sandbox_settings(),
        accounting=accounting,
    )
    service._retry_sleep, service._lease_sleep = RecordingSleep(), heartbeat  # type: ignore[assignment]
    task = asyncio.create_task(service.execute_outcome(session, run.id))
    try:
        await running_entered.wait()
        await _fail_one_heartbeat_tick(heartbeat)
        running_release.set()
        outcome = await task
        assert outcome.status is CodeRunStatus.KILLED
        assert outcome.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        assert execute_body_ran is False
        assert runner.executions == []
        assert runner.kills == 1
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.KILLED
        assert stored.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        finished = list(
            await session.scalars(
                select(models.AuditEvent).where(
                    models.AuditEvent.action == "code_run.finished",
                    models.AuditEvent.outcome == "error",
                )
            )
        )
        assert len(finished) == 1
        assert finished[0].event_metadata["reason_code"] == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        allowed = list(
            await session.scalars(
                select(models.AuditEvent).where(
                    models.AuditEvent.action == "code_run.cancelled",
                    models.AuditEvent.outcome == "allowed",
                )
            )
        )
        assert allowed == []
        assert accounting.settlements == accounting.releases == 1
    finally:
        running_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_user_stop_takes_precedence_over_failing_renewal_tick(
    session: AsyncSession,  # noqa: F811 - shared fixture
) -> None:
    """R4-001/MF: Stop plus a failing renewal tick -> cancelled, allowed audit."""
    heartbeat = BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]
    accounting = _failing_accounting(clock)
    entered, cancel_seen = asyncio.Event(), asyncio.Event()
    cancel_release = asyncio.Event()

    class Runner(_FakeRunner):
        kills = 0

        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            self.executions.append((value, run))
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancel_seen.set()
                while not cancel_release.is_set():
                    try:
                        await cancel_release.wait()
                    except asyncio.CancelledError:
                        continue
                raise
            raise AssertionError("unreachable")

        async def cancel(self, value: object, execution_id: UUID) -> None:
            self.kills += 1

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    runner = Runner()
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        object_store=_FakeStore(),
        settings=sandbox_settings(),
        accounting=accounting,
    )
    service._retry_sleep, service._lease_sleep = RecordingSleep(), heartbeat  # type: ignore[assignment]
    task = asyncio.create_task(service.execute_outcome(session, run.id))
    try:
        await entered.wait()
        task.cancel()
        await cancel_seen.wait()
        await _fail_one_heartbeat_tick(heartbeat)
        cancel_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.kills == 1
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.KILLED
        assert stored.termination_reason == "cancelled"
        allowed = list(
            await session.scalars(
                select(models.AuditEvent).where(
                    models.AuditEvent.action == "code_run.cancelled",
                    models.AuditEvent.outcome == "allowed",
                )
            )
        )
        assert len(allowed) == 1
        finished = list(
            await session.scalars(
                select(models.AuditEvent).where(models.AuditEvent.action == "code_run.finished")
            )
        )
        assert finished == []
        assert accounting.settlements == accounting.releases == 1
    finally:
        cancel_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_deferred_lease_follows_finalizer_and_expires_after_producer_death() -> None:
    from app.sandbox.admission import AdmissionLease

    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]
    accounting = CountingAccounting(now=lambda: clock[0])
    tenant, run, token = uuid4(), uuid4(), uuid4()
    assert await accounting.acquire_lease(
        tenant, run, max_concurrency=1, ttl_seconds=330, owner_token=token
    )
    assert await accounting.reserve_daily(tenant, seconds=30, daily_cap_s=3600)
    heartbeat = BarrierSleep()
    ready: asyncio.Future[AdmissionLease] = asyncio.get_running_loop().create_future()

    async def original_owner() -> None:
        lease = AdmissionLease(accounting, tenant, run, token, 330, heartbeat)
        lease.pending = True
        ready.set_result(lease)
        await asyncio.Event().wait()

    original = asyncio.create_task(original_owner())
    lease = await ready
    tick = await heartbeat.waits.get()
    original.cancel()
    await asyncio.gather(original, return_exceptions=True)
    finalizer: asyncio.Task[None] | None = None
    handshake: asyncio.Task[bool] | None = None
    try:
        assert tick.cancelled(), "A dead producer must not leave an orphan heartbeat"
        adopted = asyncio.Event()

        async def live_finalizer() -> None:
            await lease.adopt()
            adopted.set()
            await asyncio.Event().wait()

        finalizer = asyncio.create_task(live_finalizer())
        handshake = asyncio.create_task(adopted.wait())
        done, _ = await asyncio.wait({finalizer, handshake}, return_when=asyncio.FIRST_COMPLETED)
        assert handshake in done, "Deferred cleanup must adopt the same fenced admission"
        for _ in range(4):
            tick = await heartbeat.waits.get()
            clock[0] += timedelta(seconds=100)
            tick.set_result(None)
        tick = await heartbeat.waits.get()
        assert not await accounting.acquire_lease(
            tenant, uuid4(), max_concurrency=1, ttl_seconds=60
        )
        assert accounting.settlements == accounting.releases == 0
        finalizer.cancel()
        await asyncio.gather(finalizer, return_exceptions=True)
        assert tick.cancelled()
        clock[0] += timedelta(seconds=331)
        assert await accounting.acquire_lease(tenant, uuid4(), max_concurrency=1, ttl_seconds=60)
        assert (await accounting.usage(tenant))[1] == 30
    finally:
        for task in (original, finalizer, handshake):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in (original, finalizer, handshake) if task is not None],
            return_exceptions=True,
        )
        await lease.stop()
