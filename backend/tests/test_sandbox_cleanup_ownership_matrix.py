"""TF3 structural matrix: one ownership object per admitted execution.

TR1-002 and TR2-001 were the same defect class twice: a live producer's
unconfirmed sandbox cleanup lost its admission lease at the original TTL.
This module injects a producer cancellation (Stop / deadline / shutdown all
manifest as producer-task cancellation at the sandbox seam) at every await
point of the cleanup lifecycle — in-flight call, kill attempt, backoff wait,
first-failure audit/commit, and the deferred finalizer — on every path that
has a sandbox (interactive chat seam, headless runs via the shared
composition root, standalone/background service), with and without an
accounting loss latched first. Every live-producer cell asserts the same
contract: no second execution is admitted under a cap of one while the first
is unconfirmed (even after the injected clock crosses the original TTL),
exactly one settle and one release after confirmation, exactly one error
audit per execution, no allowed ``code_run.cancelled`` for an accounting-loss
kill, and the documented first-cause terminal classification (spec 0028).
Dead-producer cells assert the opposite recovery: heartbeat stops, no
refund, TTL expiry recovers the slot.

Deterministic barriers, recorded sleeps and injected clocks only.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.db import models
from app.db.repositories import CodeRunRepository
from app.domain.code_execution import SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
from app.domain.entities import CodeRunStatus
from app.realtime.backplane import InMemoryBackplane
from app.sandbox.accounting import AccountingUnavailable
from app.sandbox.runner import SandboxRunnerUnavailable
from app.sandbox.service import SandboxService
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec
from app.services.tools.types import ToolCancellationPending
from tests.test_sandbox_pending_cleanup import CountingAccounting
from tests.test_sandbox_tool_runner import _BarrierSleep, _FakeRunner, _World
from tests.test_sandbox_tool_runner import world as world  # noqa: F401 - fixture


async def _audits(world: _World, action: str, outcome: str) -> list[models.AuditEvent]:  # noqa: F811
    return list(
        await world.session.scalars(
            select(models.AuditEvent).where(
                models.AuditEvent.action == action,
                models.AuditEvent.outcome == outcome,
            )
        )
    )


async def test_probe_chat_stop_during_accounting_backoff_retains_lease(world: _World) -> None:  # noqa: F811
    """TR2-001 probe, verbatim scenario (review-TR2-chat-probes.py).

    Composition: accounting blip kills in flight, kill fails, user Stop lands in
    the service backoff wait. The chat producer is alive and keeps retrying the kill
    (ToolCancellationPending) -> the same admission must stay held (spec 0028).
    Extended past the probe: after the runner recovers, the deferred cleanup
    confirms on the same identity, settling/releasing exactly once, with the
    accounting (first-cause) classification and one shared error audit.
    """
    heartbeat, retry = _BarrierSleep(), _BarrierSleep()
    entered = asyncio.Event()
    clock = [datetime.now(UTC)]

    class Accounting(CountingAccounting):
        fail_next = True

        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            if self.fail_next:
                self.fail_next = False
                raise AccountingUnavailable("transient blip")
            await super().renew_lease(*args, **kwargs)

    class Runner(_FakeRunner):
        remote_running = True

        def __init__(self) -> None:
            super().__init__()
            self.kills: list[uuid.UUID] = []

        async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
            self.kills.append(execution_id)
            if self.remote_running:
                raise SandboxRunnerUnavailable("runner unreachable")

    accounting = Accounting(now=lambda: clock[0])
    runner = Runner()
    seam = world.seam(
        runner,
        backplane=InMemoryBackplane(),
        accounting=accounting,
        lease_sleep=heartbeat,
        retry_sleep=retry,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    pending: ToolCancellationPending | None = None
    try:
        await entered.wait()
        (await heartbeat.waits.get()).set_result(None)  # blip: accounting_lost, kill in-flight
        tick = await heartbeat.waits.get()
        await retry.waits.get()  # service accounting-loss loop: 1st kill failed, backing off
        # Accounting recovered: the live service loop still renews the same lease.
        clock[0] += timedelta(seconds=100)
        tick.set_result(None)
        tick = await heartbeat.waits.get()
        run_id = runner.kills[0]
        assert (await accounting.usage(world.tenant_id))[0] == 1
        # User Stop lands in the backoff wait.
        task.cancel()
        with pytest.raises(ToolCancellationPending) as info:
            await task
        pending = info.value
        # The chat producer is alive and retrying the SAME execution's kill.
        with pytest.raises(SandboxRunnerUnavailable):
            await pending.cleanup()
        # The deferred cleanup adopted the lease: if the dead submit
        # task's heartbeat was stopped, adoption restarted it under this
        # live finalizer, so take its fresh parked tick.
        if tick.done():
            tick = await asyncio.wait_for(heartbeat.waits.get(), 10)
        # Cross the original TTL while cleanup is still unconfirmed.
        for _ in range(4):
            clock[0] += timedelta(seconds=100)
            tick.set_result(None)
            tick = await heartbeat.waits.get()
        extra = await accounting.acquire_lease(
            world.tenant_id,
            uuid.uuid4(),
            max_concurrency=1,
            ttl_seconds=330,
            owner_token=uuid.uuid4(),
        )
        stored = await CodeRunRepository(world.session, world.tenant_id).get(run_id)
        assert not extra, "Live unconfirmed cleanup must retain its slot beyond the original TTL"
        assert stored is not None and stored.status is CodeRunStatus.RUNNING
        assert accounting.settlements == accounting.releases == 0
        assert len(await _audits(world, "code_run.cancelled", "error")) == 1
        # The runner recovers: the same deferred cleanup now confirms.
        runner.remote_running = False
        await pending.cleanup()
        assert accounting.settlements == accounting.releases == 1
        stored = await CodeRunRepository(world.session, world.tenant_id).get(run_id)
        assert stored is not None and stored.status is CodeRunStatus.KILLED
        # First established cause wins: accounting loss latched before Stop.
        assert stored.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        assert await _audits(world, "code_run.cancelled", "allowed") == []
        assert len(await _audits(world, "code_run.finished", "error")) == 1
        assert len(await _audits(world, "code_run.cancelled", "error")) == 1
        assert set(runner.kills) == {run_id}
        assert tick.cancelled()  # heartbeat stops only after confirmation
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- Shared chat-seam driver -------------------------------------------------
#
# Cells: producer cancellation injected at the in-flight call, inside a kill
# attempt, inside the first-failure audit/commit bookkeeping, or handed to a
# non-producer finalizer — with and without an accounting loss latched first.
# (accounting + backoff is the named TR2-001 probe above.) Stop, the answer
# deadline and shutdown all reach this seam as producer-task cancellation;
# ChatRuntime-level kind cells are at the end of this module.


class _SeamRig:
    def __init__(self, *, accounting_loss: bool, inject: str) -> None:
        self.accounting_loss = accounting_loss
        self.inject = inject
        self.clock = [datetime.now(UTC)]
        self.heartbeat = _BarrierSleep()
        self.retry = _BarrierSleep()
        self.entered = asyncio.Event()
        self.kill_entered = asyncio.Event()
        self.kill_release = asyncio.Event()
        self.audit_entered = asyncio.Event()
        self.audit_release = asyncio.Event()
        if inject != "kill_attempt":
            self.kill_release.set()
        if inject != "audit_commit":
            self.audit_release.set()
        rig = self

        class Accounting(CountingAccounting):
            fail_next = accounting_loss

            async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise AccountingUnavailable("transient blip")
                await super().renew_lease(*args, **kwargs)

        class Runner(_FakeRunner):
            remote_running = True

            def __init__(self) -> None:
                super().__init__()
                self.kills: list[uuid.UUID] = []

            async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
                rig.entered.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
                self.kills.append(execution_id)
                if len(self.kills) == 1:
                    rig.kill_entered.set()
                    await rig.kill_release.wait()
                if self.remote_running:
                    raise SandboxRunnerUnavailable("runner unreachable")

        self.accounting = Accounting(now=lambda: self.clock[0])
        self.runner = Runner()
        self.tick: asyncio.Future[None] | None = None

    def park_first_error_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.domain.audit import AuditAction
        from app.domain.entities import AuditOutcome
        from app.services.audit import AuditSink

        original = AuditSink.emit
        rig = self
        parked = False

        async def parking_emit(sink: AuditSink, **kwargs: Any) -> Any:
            nonlocal parked
            if (
                not parked
                and kwargs.get("action") is AuditAction.CODE_RUN_CANCELLED
                and kwargs.get("outcome") is AuditOutcome.ERROR
            ):
                parked = True
                rig.audit_entered.set()
                await rig.audit_release.wait()
            return await original(sink, **kwargs)

        monkeypatch.setattr(AuditSink, "emit", parking_emit)

    async def next_tick(self) -> asyncio.Future[None]:
        # wait_for is only a deadlock guard (as in the recovery suite): a
        # missing heartbeat must fail the cell, never hang it.
        tick = await asyncio.wait_for(self.heartbeat.waits.get(), 10)
        while tick.cancelled():
            tick = await asyncio.wait_for(self.heartbeat.waits.get(), 10)
        self.tick = tick
        return tick

    async def cross_ttl(self, seam_world: _World) -> None:
        """Advance past the original 330s TTL, renewing through live ticks."""
        if self.tick is None or self.tick.done():
            await self.next_tick()
        assert self.tick is not None
        for _ in range(4):
            self.clock[0] += timedelta(seconds=100)
            self.tick.set_result(None)
            await self.next_tick()
        extra = await self.accounting.acquire_lease(
            seam_world.tenant_id,
            uuid.uuid4(),
            max_concurrency=1,
            ttl_seconds=330,
            owner_token=uuid.uuid4(),
        )
        assert not extra, "Live unconfirmed cleanup must retain its slot beyond the original TTL"
        assert self.accounting.settlements == self.accounting.releases == 0

    async def assert_terminal(self, seam_world: _World, run_id: uuid.UUID) -> None:
        assert self.accounting.settlements == self.accounting.releases == 1
        stored = await CodeRunRepository(seam_world.session, seam_world.tenant_id).get(run_id)
        assert stored is not None and stored.status is CodeRunStatus.KILLED
        assert set(self.runner.kills) == {run_id}
        assert len(await _audits(seam_world, "code_run.cancelled", "error")) == 1
        if self.accounting_loss:
            assert stored.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
            assert await _audits(seam_world, "code_run.cancelled", "allowed") == []
            assert len(await _audits(seam_world, "code_run.finished", "error")) == 1
        else:
            assert stored.termination_reason == "cancelled"
            assert len(await _audits(seam_world, "code_run.cancelled", "allowed")) == 1


@pytest.mark.parametrize(
    ("accounting_loss", "inject"),
    [
        (False, "inflight"),
        (False, "kill_attempt"),
        (False, "audit_commit"),
        (True, "inflight"),
        (True, "kill_attempt"),
        (True, "audit_commit"),
        (True, "finalizer"),
    ],
)
async def test_chat_seam_cancellation_matrix(
    world: _World,  # noqa: F811 - shared fixture
    monkeypatch: pytest.MonkeyPatch,
    accounting_loss: bool,
    inject: str,
) -> None:
    rig = _SeamRig(accounting_loss=accounting_loss, inject=inject)
    if inject == "audit_commit":
        rig.park_first_error_audit(monkeypatch)
    seam = world.seam(
        rig.runner,
        backplane=InMemoryBackplane(),
        accounting=rig.accounting,
        lease_sleep=rig.heartbeat,
        retry_sleep=rig.retry,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    pending: ToolCancellationPending | None = None
    try:
        await rig.entered.wait()
        await rig.next_tick()
        assert rig.tick is not None
        if accounting_loss:
            rig.tick.set_result(None)  # renewal blip latches accounting loss
            await rig.next_tick()
        if inject == "inflight" and accounting_loss:
            backoff = await rig.retry.waits.get()  # producer alive in the loop
            await rig.cross_ttl(world)
            rig.runner.remote_running = False
            backoff.set_result(None)
            summary = await task
            assert summary.status is CodeRunStatus.KILLED
            assert summary.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
            await rig.assert_terminal(world, summary.code_run_id)
            return
        if accounting_loss and inject == "finalizer":
            # Deterministic, as in the TR2-001 probe: let the service loop
            # reach its backoff wait, then Stop lands exactly there.
            await rig.retry.waits.get()
            task.cancel()
            with pytest.raises(ToolCancellationPending) as info:
                await task
            await _drive_finalizer(world, rig, info.value)
            return
        if inject == "kill_attempt":
            if not accounting_loss:
                task.cancel()  # producer Stop in flight; the kill then parks
            await rig.kill_entered.wait()
            task.cancel()  # Stop / deadline / shutdown inside the kill attempt
        elif inject == "audit_commit":
            if not accounting_loss:
                task.cancel()
            await rig.audit_entered.wait()
            task.cancel()  # interrupts the first-failure audit/commit
        elif inject in ("inflight", "finalizer"):
            task.cancel()
        if accounting_loss and inject == "kill_attempt":
            # Absorbed by the service loop as a failed attempt; the producer
            # stays in backoff on the same lease, classification latched.
            backoff = await rig.retry.waits.get()
            await rig.cross_ttl(world)
            rig.runner.remote_running = False
            backoff.set_result(None)
            with pytest.raises(asyncio.CancelledError):
                await task
            await rig.assert_terminal(world, rig.runner.kills[0])
            return
        with pytest.raises(ToolCancellationPending) as info:
            await task
        pending = info.value
        run_id = rig.runner.kills[0]
        if inject == "finalizer":
            await _drive_finalizer(world, rig, pending)
            return
        with pytest.raises(SandboxRunnerUnavailable):
            await pending.cleanup()
        await rig.cross_ttl(world)
        stored = await CodeRunRepository(world.session, world.tenant_id).get(run_id)
        assert stored is not None and stored.status is CodeRunStatus.RUNNING
        rig.runner.remote_running = False
        await pending.cleanup()
        await rig.assert_terminal(world, run_id)
    finally:
        rig.kill_release.set()
        rig.audit_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _drive_finalizer(
    seam_world: _World, rig: _SeamRig, pending: ToolCancellationPending
) -> None:
    """TR2-003: cleanup driven by a task other than the dead producer.

    Fails if ``confirmed_cleanup`` stops adopting the lease: without
    adoption the dead producer's heartbeat stays cancelled, no tick is
    parked under the finalizer, and the TTL crossing admits a second
    execution.
    """
    first_attempt_done = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def finalize() -> None:
        with pytest.raises(SandboxRunnerUnavailable):
            await pending.cleanup()
        first_attempt_done.set()
        await release_finalizer.wait()
        await pending.cleanup()

    finalizer = asyncio.create_task(finalize())
    try:
        await first_attempt_done.wait()
        assert not rig.heartbeat.waits.empty() or (
            rig.tick is not None and not rig.tick.done()
        ), "Adopted lease must keep a heartbeat parked under the live finalizer"
        await rig.cross_ttl(seam_world)
        rig.runner.remote_running = False
        release_finalizer.set()
        await finalizer
        await rig.assert_terminal(seam_world, rig.runner.kills[0])
    finally:
        release_finalizer.set()
        finalizer.cancel()
        await asyncio.gather(finalizer, return_exceptions=True)


async def test_confirmed_cleanup_adopts_lease_for_non_producer_finalizer(
    world: _World,  # noqa: F811 - shared fixture
) -> None:
    """TR2-003 named seam regression (Stop in flight, foreign finalizer)."""
    rig = _SeamRig(accounting_loss=False, inject="finalizer")
    seam = world.seam(
        rig.runner,
        backplane=InMemoryBackplane(),
        accounting=rig.accounting,
        lease_sleep=rig.heartbeat,
        retry_sleep=rig.retry,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    try:
        await rig.entered.wait()
        await rig.next_tick()
        task.cancel()
        with pytest.raises(ToolCancellationPending) as info:
            await task
        assert task.done()  # the producer is genuinely gone
        await _drive_finalizer(world, rig, info.value)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- Standalone / background: the dead-producer cells ------------------------
#
# With no chat callback, a cancellation that escapes the service loop
# (backoff wait or audit/commit bookkeeping) IS producer death: the
# heartbeat stops, nothing is settled or released (no refund), and TTL
# expiry is the only recovery. The live absorbed cells (in-flight and
# interrupted kill attempts, standalone and background) are the existing
# eight-case matrix in test_sandbox_pending_cleanup.

from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from tests._sandbox_helpers import sandbox_settings  # noqa: E402
from tests.test_sandbox_limits import _FakeRunner as _SvcRunner  # noqa: E402
from tests.test_sandbox_limits import _FakeStore as _SvcStore  # noqa: E402
from tests.test_sandbox_limits import _setup  # noqa: E402
from tests.test_sandbox_limits import session as session  # noqa: E402, F401
from tests.test_sandbox_pending_cleanup import BarrierSleep  # noqa: E402
from tests.test_sandbox_pending_cleanup import admission_day as admission_day  # noqa: E402, F401


@pytest.mark.parametrize("accounting_loss", [False, True])
@pytest.mark.parametrize("inject", ["backoff", "audit_commit"])
@pytest.mark.parametrize("background", [False, True])
async def test_standalone_dead_producer_matrix(
    session: AsyncSession,  # noqa: F811 - shared fixture
    monkeypatch: pytest.MonkeyPatch,
    accounting_loss: bool,
    inject: str,
    background: bool,
) -> None:
    from app.tasks import run_sandbox

    entered = asyncio.Event()
    retry, heartbeat = BarrierSleep(), BarrierSleep()
    clock = [datetime(2026, 10, 30, 12, tzinfo=UTC)]

    class Accounting(CountingAccounting):
        fail_next = accounting_loss

        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            if self.fail_next:
                self.fail_next = False
                raise AccountingUnavailable("transient blip")
            await super().renew_lease(*args, **kwargs)

    accounting = Accounting(now=lambda: clock[0])

    class Runner(_SvcRunner):
        remote_running = True
        kills: list[tuple[SandboxSessionSpec, UUID]] = []

        async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
            self.executions.append((value, run))
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: UUID) -> None:
            self.kills.append((value, execution_id))
            raise SandboxRunnerUnavailable("runner unreachable")

    audit_entered, audit_release = asyncio.Event(), asyncio.Event()
    if inject == "audit_commit":
        from app.domain.audit import AuditAction
        from app.domain.entities import AuditOutcome
        from app.services.audit import AuditSink

        original_emit = AuditSink.emit
        parked = False

        async def parking_emit(sink: AuditSink, **kwargs: Any) -> Any:
            nonlocal parked
            if (
                not parked
                and kwargs.get("action") is AuditAction.CODE_RUN_CANCELLED
                and kwargs.get("outcome") is AuditOutcome.ERROR
            ):
                parked = True
                audit_entered.set()
                await audit_release.wait()
            return await original_emit(sink, **kwargs)

        monkeypatch.setattr(AuditSink, "emit", parking_emit)
    else:
        audit_release.set()

    tenant, owner, chat = await _setup(session, concurrency=1)
    run = await CodeRunRepository(session, tenant).create(
        owner_id=owner, session_id=chat, code="pass"
    )
    runner = Runner()
    service = SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        object_store=_SvcStore(),
        settings=sandbox_settings(),
        accounting=accounting,
    )
    service._retry_sleep, service._lease_sleep = retry, heartbeat  # type: ignore[assignment]

    @asynccontextmanager
    async def scope(_tenant: UUID) -> AsyncIterator[AsyncSession]:
        yield session

    if background:
        monkeypatch.setattr(run_sandbox, "tenant_session_scope", scope)
        monkeypatch.setattr(run_sandbox, "HttpSandboxRunner", lambda *a, **kw: runner)
        monkeypatch.setattr(run_sandbox, "ObjectStore", lambda *a: _SvcStore())
        monkeypatch.setattr(run_sandbox, "SandboxService", lambda **kw: service)
        work = run_sandbox.execute_code_run_async(run.id, tenant, settings=sandbox_settings())
    else:
        work = service.execute_outcome(session, run.id)
    task = asyncio.create_task(work)
    try:
        await entered.wait()
        tick = await heartbeat.waits.get()
        if accounting_loss:
            tick.set_result(None)
            tick = await heartbeat.waits.get()
        else:
            task.cancel()
        if inject == "backoff":
            await retry.waits.get()
        else:
            await audit_entered.wait()
        task.cancel()  # the producer dies here; nobody continues cleanup
        with pytest.raises(asyncio.CancelledError):
            await task
        # Dead-producer contract: no settle, no release, heartbeat stopped,
        # reservation still charged; only TTL expiry recovers the slot.
        assert accounting.settlements == accounting.releases == 0
        assert tick.cancelled()
        assert (await accounting.usage(tenant))[1] == 30
        stored = await CodeRunRepository(session, tenant).get(run.id)
        assert stored is not None and stored.status is CodeRunStatus.RUNNING
        errors = list(
            await session.scalars(
                select(models.AuditEvent).where(
                    models.AuditEvent.action == "code_run.cancelled",
                    models.AuditEvent.outcome == "error",
                )
            )
        )
        assert len(errors) == (1 if inject == "backoff" else 0)
        clock[0] += timedelta(seconds=400)  # past the 330s TTL, no renewals
        assert await accounting.acquire_lease(
            tenant, uuid4(), max_concurrency=1, ttl_seconds=60
        ), "TTL expiry must recover a genuinely dead producer's slot"
        assert accounting.settlements == accounting.releases == 0
    finally:
        audit_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- Headless runs via the shared composition root ----------------------------
#
# The seam comes from build_chat_runtime(mode="headless")'s sandbox factory
# (the composition root shared with interactive chat), with the runner and
# accountant injected exactly as the existing headless composition tests do.

from functools import partial  # noqa: E402

from app.db.repositories import TenantSandboxPolicyRepository  # noqa: E402
from app.sandbox.tool_runner import ChatSandboxToolRunner  # noqa: E402
from app.services.chat_composition import ChatCompositionOptions, build_chat_runtime  # noqa: E402
from app.services.chat_runtime import SandboxContext  # noqa: E402
from tests.test_chat_composition import _World as _CompWorld  # noqa: E402
from tests.test_chat_composition import world as comp_world  # noqa: E402, F401


@pytest.mark.parametrize("accounting_loss", [False, True])
async def test_headless_composition_matrix(
    comp_world: _CompWorld,  # noqa: F811 - shared fixture
    monkeypatch: pytest.MonkeyPatch,
    accounting_loss: bool,
) -> None:
    rig = _SeamRig(
        accounting_loss=accounting_loss, inject="backoff" if accounting_loss else "inflight"
    )
    monkeypatch.setattr(
        "app.services.chat_composition.HttpSandboxRunner", lambda *a, **kw: rig.runner
    )
    monkeypatch.setattr(
        "app.services.chat_composition.ChatSandboxToolRunner",
        partial(
            ChatSandboxToolRunner,
            accounting=rig.accounting,
            retry_sleep=rig.retry,
            lease_sleep=rig.heartbeat,
        ),
    )
    async with comp_world.sessionmaker() as session:  # noqa: F811
        await TenantSandboxPolicyRepository(session, comp_world.tenant_id).upsert(
            enabled=True,
            allowed_packages=(),
            denied_packages=(),
            egress_allowed=False,
            egress_allowlist=(),
            max_runtime_s=30,
            max_memory_mb=512,
            daily_runtime_cap_s=3600,
            max_concurrency=1,
            updated_by=comp_world.alice_id,
        )
        await session.commit()
    runtime = build_chat_runtime(
        mode="headless",
        principal=comp_world.alice,
        backplane=InMemoryBackplane(),
        settings=sandbox_settings(),
        sessionmaker=comp_world.sessionmaker,
        gateway=object(),
        request_id="req-tf3",
        source_ip="127.0.0.1",
        options=ChatCompositionOptions(object_store_factory=lambda: comp_world.store),
    )
    assert runtime._sandbox_factory is not None
    seam = runtime._sandbox_factory(
        SandboxContext(
            stream_id="headless",
            session_id=comp_world.session_id,
            message_id=uuid.uuid4(),
            next_seq=lambda: 0,
        )
    )
    assert seam is not None
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    try:
        await rig.entered.wait()
        await rig.next_tick()
        assert rig.tick is not None
        if accounting_loss:
            rig.tick.set_result(None)
            await rig.next_tick()
            await rig.retry.waits.get()  # service backoff, kill unconfirmed
        task.cancel()
        with pytest.raises(ToolCancellationPending) as info:
            await task
        pending = info.value
        with pytest.raises(SandboxRunnerUnavailable):
            await pending.cleanup()

        class _Shim:  # rig helpers take the tool-runner world shape
            tenant_id = comp_world.tenant_id

        await rig.cross_ttl(_Shim())  # type: ignore[arg-type]
        run_id = rig.runner.kills[0]
        rig.runner.remote_running = False
        await pending.cleanup()
        assert rig.accounting.settlements == rig.accounting.releases == 1
        async with comp_world.sessionmaker() as check:
            stored = await CodeRunRepository(check, comp_world.tenant_id).get(run_id)
            assert stored is not None and stored.status is CodeRunStatus.KILLED
            assert stored.termination_reason == (
                SANDBOX_REASON_ACCOUNTING_UNAVAILABLE if accounting_loss else "cancelled"
            )
            errors = list(
                await check.scalars(
                    select(models.AuditEvent).where(
                        models.AuditEvent.action == "code_run.cancelled",
                        models.AuditEvent.outcome == "error",
                    )
                )
            )
            assert len(errors) == 1
            allowed = list(
                await check.scalars(
                    select(models.AuditEvent).where(
                        models.AuditEvent.action == "code_run.cancelled",
                        models.AuditEvent.outcome == "allowed",
                    )
                )
            )
            assert len(allowed) == (0 if accounting_loss else 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- ChatRuntime kinds: Stop vs shutdown, and the TR2-002 ceiling -----------
#
# Producer-level cells through the real ChatRuntime: Stop keeps the partial
# answer marked cancelled only after cleanup confirms; shutdown settles via
# the retryable error terminal and re-raises. The answer deadline manifests
# at this layer as the same producer-task cancellation (its terminal-code
# selection is covered by test_cancellation_regressions); the sandbox seam
# cells above inject that cancellation. The code run's classification
# follows the first established cause while the turn reflects the kind.

from app.db.repositories import TenantToolPolicyRepository  # noqa: E402
from app.domain.llm import StreamEvent, ToolCall  # noqa: E402
from app.realtime.backplane import StreamOwner, is_terminal  # noqa: E402
from app.services.assistant_runtime import AssistantRunConfig  # noqa: E402
from app.services.chat_runtime import ChatRuntime  # noqa: E402
from tests.test_cancellation_regressions import _run  # noqa: E402
from tests.test_cancellation_regressions import ctx as ctx  # noqa: E402, F401
from tests.test_cancellation_regressions import (  # noqa: E402, F401
    deterministic_token_counts as deterministic_token_counts,
)
from tests.test_chat_runtime import _Ctx, _FakeRetrieval, _ScriptedGateway  # noqa: E402
from tests.test_sandbox_tool_runner import _FakeStore  # noqa: E402


class _RuntimeRig:
    def __init__(self, *, accounting_loss: bool, failures: int) -> None:
        self.clock = {"now": datetime(2026, 10, 30, 12, tzinfo=UTC)}
        self.heartbeat = BarrierSleep()
        self.service_retry = BarrierSleep()
        self.entered = asyncio.Event()
        self.retry_delays: list[float] = []
        self.retries: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()
        self.barriers: list[asyncio.Future[None]] = []
        self.tearing_down = False
        rig = self

        class Accounting(CountingAccounting):
            fail_next = accounting_loss

            async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise AccountingUnavailable("transient blip")
                await super().renew_lease(*args, **kwargs)

        class Runner(_FakeRunner):
            remote_running = False

            def __init__(self) -> None:
                super().__init__()
                self.kills: list[uuid.UUID] = []

            async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
                self.remote_running = True
                rig.entered.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
                self.kills.append(execution_id)
                if len(self.kills) <= failures:
                    raise SandboxRunnerUnavailable("runner unreachable")
                self.remote_running = False

        self.accounting = Accounting(now=lambda: self.clock["now"])
        self.runner = Runner()

    async def retry_barrier(self, delay: float) -> None:
        self.retry_delays.append(delay)
        if self.tearing_down:
            return
        release = asyncio.get_running_loop().create_future()
        self.barriers.append(release)
        self.retries.put_nowait(release)
        await release

    async def cross_ttl(self, tenant_id: UUID) -> None:
        for _ in range(4):
            tick = await asyncio.wait_for(self.heartbeat.waits.get(), 10)
            while tick.cancelled():
                tick = await asyncio.wait_for(self.heartbeat.waits.get(), 10)
            self.clock["now"] += timedelta(seconds=100)
            tick.set_result(None)
        tick = await asyncio.wait_for(self.heartbeat.waits.get(), 10)
        while tick.cancelled():
            tick = await asyncio.wait_for(self.heartbeat.waits.get(), 10)
        self.heartbeat.waits.put_nowait(tick)
        assert not await self.accounting.acquire_lease(
            tenant_id, uuid.uuid4(), max_concurrency=1, ttl_seconds=60
        ), "Live unconfirmed cleanup must retain its slot beyond the original TTL"
        assert self.accounting.settlements == 0

    def teardown(self) -> None:
        self.tearing_down = True
        for barrier in self.barriers:
            if not barrier.done():
                barrier.set_result(None)


async def _runtime_producer(
    ctx: _Ctx,  # noqa: F811
    rig: _RuntimeRig,
) -> tuple[asyncio.Task[bool], ChatRuntime, InMemoryBackplane, str]:
    async with ctx.sessionmaker() as session:  # noqa: F811
        await TenantSandboxPolicyRepository(session, ctx.tenant_id).upsert(
            enabled=True,
            allowed_packages=(),
            denied_packages=(),
            egress_allowed=False,
            egress_allowlist=(),
            max_runtime_s=30,
            max_memory_mb=512,
            daily_runtime_cap_s=3600,
            max_concurrency=1,
            updated_by=ctx.principal.user_id,
        )
        await TenantToolPolicyRepository(session, ctx.tenant_id).upsert(
            tool_name="run_python",
            enabled=True,
            requires_approval=False,
            updated_by=ctx.principal.user_id,
        )
        await session.commit()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    await backplane.bind_owner(stream_id, StreamOwner(ctx.principal.user_id, ctx.tenant_id))

    def sandbox_factory(context: SandboxContext) -> ChatSandboxToolRunner:
        tool_runner = ChatSandboxToolRunner(
            sessionmaker=ctx.sessionmaker,
            tenant_id=ctx.tenant_id,
            owner_id=ctx.principal.user_id,
            runner=rig.runner,
            accounting=rig.accounting,
            object_store=_FakeStore(),  # type: ignore[arg-type]
            settings=sandbox_settings(),
            backplane=backplane,
            stream_id=context.stream_id,
            session_id=context.session_id,
            message_id=context.message_id,
            next_seq=context.next_seq,
            request_id="tf3-matrix",
            source_ip="unknown",
        )
        tool_runner._lease_sleep = rig.heartbeat
        tool_runner._retry_sleep = rig.service_retry
        return tool_runner

    runtime = ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        principal=ctx.principal,
        gateway=_ScriptedGateway(
            [[StreamEvent(tool_calls=(ToolCall("code", "run_python", {"code": "pass"}),))]]
        ),  # type: ignore[arg-type]
        backplane=backplane,
        request_id="tf3-matrix",
        source_ip="unknown",
        retrieval_factory=lambda _session: _FakeRetrieval([]),  # type: ignore[arg-type,return-value]
        sandbox_factory=sandbox_factory,
        retry_sleep=rig.retry_barrier,
    )
    producer = asyncio.create_task(
        _run(
            runtime,
            ctx,
            stream_id,
            assistant_config=AssistantRunConfig(
                system_prompt="Compute the answer.",
                allowed=frozenset({"run_python"}),
                collection_ids=None,
                model=None,
            ),
        )
    )
    return producer, runtime, backplane, stream_id


@pytest.mark.parametrize("accounting_loss", [False, True])
@pytest.mark.parametrize("kind", ["stop", "shutdown"])
async def test_runtime_cancellation_kind_matrix(
    ctx: _Ctx,  # noqa: F811 - shared fixture
    accounting_loss: bool,
    kind: str,
) -> None:
    rig = _RuntimeRig(accounting_loss=accounting_loss, failures=2)
    producer, runtime, backplane, stream_id = await _runtime_producer(ctx, rig)
    pending_retry: asyncio.Task[asyncio.Future[None]] | None = None
    try:
        await asyncio.wait_for(rig.entered.wait(), 30)
        if accounting_loss:
            tick = await rig.heartbeat.waits.get()
            tick.set_result(None)  # accounting loss latched first
            await rig.service_retry.waits.get()  # kill failed; service in backoff
        if kind == "stop":
            runtime.request_cancel()
        producer.cancel()
        pending_retry = asyncio.create_task(rig.retries.get())
        done, _ = await asyncio.wait(
            {producer, pending_retry}, timeout=30, return_when=asyncio.FIRST_COMPLETED
        )
        assert pending_retry in done, "Unconfirmed kill must stay pending in the producer"
        release = pending_retry.result()
        assert rig.runner.remote_running
        assert await backplane.get_terminal(stream_id) is None
        await rig.cross_ttl(ctx.tenant_id)
        async with ctx.sessionmaker() as check:
            run = (await check.scalars(select(models.CodeRun))).one()
            assert run.status == CodeRunStatus.RUNNING.value
            errors = list(
                await check.scalars(
                    select(models.AuditEvent).where(
                        models.AuditEvent.action == "code_run.cancelled",
                        models.AuditEvent.outcome == "error",
                    )
                )
            )
            assert len(errors) == 1  # one shared first-failure audit
        release.set_result(None)
        # Second retry confirms (runner failures exhausted after one more).
        pending_retry = asyncio.create_task(rig.retries.get())
        done, _ = await asyncio.wait(
            {producer, pending_retry}, timeout=30, return_when=asyncio.FIRST_COMPLETED
        )
        if pending_retry in done:
            pending_retry.result().set_result(None)
        if kind == "stop":
            assert await asyncio.wait_for(asyncio.shield(producer), 30) is False
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(producer), 30)
        assert not rig.runner.remote_running
        assert rig.accounting.settlements == rig.accounting.releases == 1
        events = backplane._replay[stream_id]
        terminals = [event["type"] for event in events if is_terminal(event)]
        assert terminals == (["cancelled"] if kind == "stop" else ["error"])
        async with ctx.sessionmaker() as check:
            run = (await check.scalars(select(models.CodeRun))).one()
            assert run.status == CodeRunStatus.KILLED.value
            # First cause wins for the run; the turn reflects the kind above.
            assert run.termination_reason == (
                SANDBOX_REASON_ACCOUNTING_UNAVAILABLE if accounting_loss else "cancelled"
            )
            cancelled = list(
                await check.scalars(
                    select(models.AuditEvent).where(
                        models.AuditEvent.action == "code_run.cancelled"
                    )
                )
            )
            assert sum(event.outcome == "error" for event in cancelled) == 1
            assert sum(event.outcome == "allowed" for event in cancelled) == (
                0 if accounting_loss else 1
            )
            if kind == "stop":
                messages = list(await check.scalars(select(models.Message)))
                assert any(message.finish_reason == "cancelled" for message in messages)
    finally:
        rig.teardown()
        if pending_retry is not None:
            pending_retry.cancel()
        producer.cancel()
        await asyncio.gather(
            producer, *([pending_retry] if pending_retry else []), return_exceptions=True
        )


async def test_chat_retry_cleanup_backoff_ceiling(ctx: _Ctx) -> None:  # noqa: F811
    """TR2-002: chat-path retry_cleanup doubles to the 60s ceiling and holds it."""
    rig = _RuntimeRig(accounting_loss=False, failures=8)
    producer, runtime, backplane, stream_id = await _runtime_producer(ctx, rig)
    pending_retry: asyncio.Task[asyncio.Future[None]] | None = None
    try:
        await asyncio.wait_for(rig.entered.wait(), 30)
        runtime.request_cancel()
        producer.cancel()
        for _ in range(8):
            pending_retry = asyncio.create_task(rig.retries.get())
            done, _ = await asyncio.wait(
                {producer, pending_retry}, timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            assert pending_retry in done
            pending_retry.result().set_result(None)
        assert await asyncio.wait_for(asyncio.shield(producer), 30) is False
        assert rig.retry_delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
        assert len(rig.runner.kills) == 9
        assert rig.accounting.settlements == rig.accounting.releases == 1
    finally:
        rig.teardown()
        if pending_retry is not None:
            pending_retry.cancel()
        producer.cancel()
        await asyncio.gather(
            producer, *([pending_retry] if pending_retry else []), return_exceptions=True
        )
