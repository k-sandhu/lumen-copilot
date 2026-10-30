"""Unconfirmed-cleanup ownership survives bookkeeping failures (TR3-001) and settles once (TR3-003).

A failed kill's audit/commit bookkeeping can itself fail (a DB blip during an
accounting outage). That is one more unconfirmed attempt, never an exit: if it
escaped, the producer — still alive, the tool failure becomes a result and the
answer continues — would close the admission ownership, stop renewal and let
the slot expire while the remote execution still runs (the TR1-002/TR2-001
class through a non-cancellation exit). And a deferred cleanup whose lease
release fails must not settle the daily reservation a second time on retry.

Deterministic: barrier sleeps, an injected accountant clock; ``wait_for`` is a
deadlock guard only.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db import models
from app.db.repositories import CodeRunRepository
from app.domain.audit import AuditAction
from app.domain.code_execution import SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
from app.domain.entities import AuditOutcome, CodeRunStatus
from app.realtime.backplane import InMemoryBackplane
from app.sandbox.accounting import AccountingUnavailable
from app.sandbox.runner import SandboxRunnerUnavailable
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec
from app.services.audit import AuditSink
from app.services.tools.types import ToolCancellationPending
from tests.test_sandbox_pending_cleanup import CountingAccounting
from tests.test_sandbox_tool_runner import _BarrierSleep, _FakeRunner, _World
from tests.test_sandbox_tool_runner import world as world  # noqa: F401 - fixture


def _rig(*, fail_ticks: int) -> tuple[Any, Any, list[datetime], asyncio.Event]:
    entered = asyncio.Event()
    clock = [datetime.now(UTC)]

    class Accounting(CountingAccounting):
        fails = fail_ticks

        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            if self.fails > 0:
                self.fails -= 1
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

    return Accounting(now=lambda: clock[0]), Runner(), clock, entered


async def _audits(world: _World, action: str, outcome: str) -> list[models.AuditEvent]:  # noqa: F811
    return list(
        await world.session.scalars(
            select(models.AuditEvent).where(
                models.AuditEvent.action == action,
                models.AuditEvent.outcome == outcome,
            )
        )
    )


async def test_failed_kill_bookkeeping_error_keeps_live_ownership(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TR3-001: accounting loss -> kill fails -> its error-audit write raises.

    No cancellation is involved: the producer is alive. The service must treat
    the bookkeeping failure as one more unconfirmed attempt (back off and
    retry the same execution) and keep renewing the lease past the original
    TTL until the kill is confirmed, then settle and release exactly once.
    """
    original = AuditSink.emit
    raised = False

    async def failing_emit(sink: AuditSink, **kwargs: Any) -> Any:
        nonlocal raised
        if (
            not raised
            and kwargs.get("action") is AuditAction.CODE_RUN_CANCELLED
            and kwargs.get("outcome") is AuditOutcome.ERROR
        ):
            raised = True
            raise OperationalError("INSERT audit_events", {}, Exception("connection reset"))
        return await original(sink, **kwargs)

    monkeypatch.setattr(AuditSink, "emit", failing_emit)
    heartbeat, retry = _BarrierSleep(), _BarrierSleep()
    accounting, runner, clock, entered = _rig(fail_ticks=1)
    seam = world.seam(
        runner,
        backplane=InMemoryBackplane(),
        accounting=accounting,
        lease_sleep=heartbeat,
        retry_sleep=retry,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    try:
        await entered.wait()
        (await heartbeat.waits.get()).set_result(None)  # blip: accounting lost, in-flight killed
        tick = await asyncio.wait_for(heartbeat.waits.get(), 10)
        # The failed kill's audit raised, yet the loop reached its backoff wait.
        backoff = await asyncio.wait_for(retry.waits.get(), 10)
        assert raised
        assert not task.done()
        run_id = runner.kills[0]
        # Accounting recovered: the live producer keeps renewing past the TTL.
        for _ in range(4):
            clock[0] += timedelta(seconds=100)
            tick.set_result(None)
            tick = await asyncio.wait_for(heartbeat.waits.get(), 10)
        assert not tick.cancelled()
        extra = await accounting.acquire_lease(
            world.tenant_id,
            uuid.uuid4(),
            max_concurrency=1,
            ttl_seconds=330,
            owner_token=uuid.uuid4(),
        )
        assert not extra, "a live, unconfirmed execution lost its slot at the original TTL"
        stored = await CodeRunRepository(world.session, world.tenant_id).get(run_id)
        assert stored is not None and stored.status is CodeRunStatus.RUNNING
        assert (accounting.settlements, accounting.releases) == (0, 0)
        # The runner recovers: the retry confirms the SAME execution's kill.
        runner.remote_running = False
        backoff.set_result(None)
        summary = await asyncio.wait_for(task, 10)
        assert summary is not None
        assert set(runner.kills) == {run_id}
        assert (accounting.settlements, accounting.releases) == (1, 1)
        await world.session.rollback()
        stored = await CodeRunRepository(world.session, world.tenant_id).get(run_id)
        assert stored is not None and stored.status is CodeRunStatus.KILLED
        assert stored.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        assert not await _audits(world, "code_run.cancelled", "allowed")
        assert len(await _audits(world, "code_run.finished", "error")) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_settlement_is_not_repeated_when_release_is_retried(
    world: _World,  # noqa: F811
) -> None:
    """TR3-003: deferred cleanup confirms the kill and settles, the lease release
    fails once, and the retry re-enters confirmation: settle exactly once."""
    heartbeat, retry = _BarrierSleep(), _BarrierSleep()
    accounting, runner, _clock, entered = _rig(fail_ticks=0)
    failures = {"release": 0}
    original_release = type(accounting).release_lease

    async def flaky_release(self: Any, *args: Any, **kwargs: Any) -> None:
        if failures["release"] > 0:
            failures["release"] -= 1
            raise AccountingUnavailable("release blip")
        await original_release(self, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(type(accounting), "release_lease", flaky_release)
    seam = world.seam(
        runner,
        backplane=InMemoryBackplane(),
        accounting=accounting,
        lease_sleep=heartbeat,
        retry_sleep=retry,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    try:
        await entered.wait()
        task.cancel()  # user Stop in flight; the callback's kill fails -> hand-off
        with pytest.raises(ToolCancellationPending) as info:
            await task
        pending = info.value
        runner.remote_running = False
        failures["release"] = 1
        with pytest.raises(AccountingUnavailable):
            await pending.cleanup()
        await pending.cleanup()
        assert accounting.settlements == 1
        assert accounting.releases == 1
    finally:
        monkeypatch.undo()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
