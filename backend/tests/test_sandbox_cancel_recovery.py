"""R1-001: an unconfirmed sandbox kill cannot acknowledge successful Stop."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.errors import DependencyError
from app.db import models
from app.db.repositories import TenantSandboxPolicyRepository, TenantToolPolicyRepository
from app.domain.entities import CodeRunStatus
from app.domain.llm import StreamEvent, ToolCall
from app.realtime.backplane import InMemoryBackplane, StreamOwner, is_terminal
from app.sandbox.accounting import InMemorySandboxAccounting
from app.sandbox.runner import SandboxRunnerUnavailable
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec
from app.sandbox.tool_runner import ChatSandboxToolRunner
from app.services import stream_cancellation
from app.services.assistant_runtime import AssistantRunConfig
from app.services.chat_runtime import ChatRuntime, SandboxContext
from app.services.stream_cancellation import StreamCancellationService
from app.services.stream_registry import StreamRegistry
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context
from tests._sandbox_helpers import sandbox_settings
from tests.test_cancellation_regressions import (
    _assert_cancelled_database,
    _run,
)
from tests.test_cancellation_regressions import (
    ctx as ctx,
)
from tests.test_cancellation_regressions import (
    deterministic_token_counts as deterministic_token_counts,
)
from tests.test_chat_runtime import _Ctx, _FakeRetrieval, _ScriptedGateway
from tests.test_sandbox_pending_cleanup import BarrierSleep
from tests.test_sandbox_pending_cleanup import admission_day as admission_day
from tests.test_sandbox_tool_runner import _FakeRunner, _FakeStore


@pytest.mark.parametrize("failures", [1, 2])
@pytest.mark.parametrize("same_replica", [False, True])
@pytest.mark.parametrize("interrupt_kill", [False, True])
async def test_failed_sandbox_kill_stays_unconfirmed_until_same_execution_is_reaped(
    ctx: _Ctx,  # noqa: F811 — imported shared fixture
    monkeypatch: pytest.MonkeyPatch,
    durable_audit_ledger: RecordingDurableAuditTransactions,
    failures: int,
    same_replica: bool,
    interrupt_kill: bool,
) -> None:
    heartbeat = BarrierSleep()
    clock = {"now": datetime(2026, 10, 30, 12, tzinfo=UTC)}
    entered = asyncio.Event()
    kill_entered = asyncio.Event()
    retries: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()
    barriers: list[asyncio.Future[None]] = []
    retry_delays: list[float] = []
    tearing_down = False
    timeouts: asyncio.Queue[asyncio.Timeout] = asyncio.Queue()
    original_timeout = asyncio.timeout

    def controlled_timeout(seconds: float) -> asyncio.Timeout:
        timeout = original_timeout(seconds)
        timeouts.put_nowait(timeout)
        return timeout

    # Only the cancel service uses asyncio.timeout here; runtime deadlines use
    # timeout_at. Rescheduling the real timeout tests 503 without a clock wait.
    monkeypatch.setattr(stream_cancellation.asyncio, "timeout", controlled_timeout)

    async def retry_barrier(_delay: float) -> None:
        retry_delays.append(_delay)
        if tearing_down:
            return
        release = asyncio.get_running_loop().create_future()
        barriers.append(release)
        retries.put_nowait(release)
        await release

    class Runner(_FakeRunner):
        remote_running = False

        def __init__(self) -> None:
            super().__init__()
            self.executions: list[uuid.UUID] = []
            self.kills: list[tuple[SandboxSessionSpec, uuid.UUID]] = []

        async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
            self.executions.append(spec.execution_id)
            self.remote_running = True
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
            self.kills.append((value, execution_id))
            if interrupt_kill and len(self.kills) == 1:
                kill_entered.set()
                await asyncio.Event().wait()
            if len(self.kills) <= failures:
                raise SandboxRunnerUnavailable("Synthetic cleanup transport failure.")
            self.remote_running = False

    async with ctx.sessionmaker() as session:
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

    class Accounting(InMemorySandboxAccounting):
        settlements = 0

        async def settle_daily(self, *args: Any, **kwargs: Any) -> None:
            await super().settle_daily(*args, **kwargs)
            self.settlements += 1

    accounting = Accounting(now=lambda: clock["now"])
    runner = Runner()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    await backplane.bind_owner(stream_id, StreamOwner(ctx.principal.user_id, ctx.tenant_id))
    settings = sandbox_settings()

    def sandbox_factory(context: SandboxContext) -> ChatSandboxToolRunner:
        tool_runner = ChatSandboxToolRunner(
            sessionmaker=ctx.sessionmaker,
            tenant_id=ctx.tenant_id,
            owner_id=ctx.principal.user_id,
            runner=runner,
            accounting=accounting,
            object_store=_FakeStore(),  # type: ignore[arg-type]
            settings=settings,
            backplane=backplane,
            stream_id=context.stream_id,
            session_id=context.session_id,
            message_id=context.message_id,
            next_seq=context.next_seq,
            request_id="sandbox-cancel-recovery",
            source_ip="unknown",
        )
        # Assign the lease-heartbeat seam before dispatch (ignored by the
        # unfixed code, which never renews pending admission).
        tool_runner._lease_sleep = heartbeat
        return tool_runner

    gateway = _ScriptedGateway(
        [[StreamEvent(tool_calls=(ToolCall("code", "run_python", {"code": "pass"}),))]]
    )
    runtime = ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        principal=ctx.principal,
        gateway=gateway,  # type: ignore[arg-type]
        backplane=backplane,
        request_id="sandbox-cancel-recovery",
        source_ip="unknown",
        retrieval_factory=lambda _session: _FakeRetrieval([]),  # type: ignore[arg-type,return-value]
        sandbox_factory=sandbox_factory,
        retry_sleep=retry_barrier,
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
    local = StreamRegistry()
    local.register(stream_id, task=producer, runtime=runtime)
    # An empty registry models Stop on a different serving replica.
    async with ctx.sessionmaker() as request_session:
        service = StreamCancellationService(
            backplane=backplane,
            registry=local if same_replica else StreamRegistry(),
            denials=denial_context(
                durable_audit_ledger, request_session, ctx.tenant_id, ctx.principal.user_id
            ),
            timeout_seconds=60,
        )
        requests: list[asyncio.Task[dict[str, Any]]] = []
        pending_retry: asyncio.Task[asyncio.Future[None]] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 30)
            for _attempt in range(failures):
                request = asyncio.create_task(
                    service.cancel(stream_id=stream_id, principal=ctx.principal)
                )
                requests.append(request)
                timeout = await asyncio.wait_for(timeouts.get(), 30)
                if interrupt_kill and _attempt == 0:
                    await asyncio.wait_for(kill_entered.wait(), 30)
                    # Shutdown/deadline cancellation may interrupt the kill HTTP
                    # request itself; that is still an unconfirmed remote kill.
                    producer.cancel()
                pending_retry = asyncio.create_task(retries.get())
                done, _ = await asyncio.wait(
                    {producer, pending_retry}, timeout=30, return_when=asyncio.FIRST_COMPLETED
                )
                assert pending_retry in done, (
                    "Failed sandbox kill must keep cleanup pending, not publish cancelled: "
                    f"{await backplane.get_terminal(stream_id)}"
                )
                release = pending_retry.result()
                assert runner.remote_running
                # TR1-002: cross the original 330-second admission TTL while
                # the live producer is parked in cleanup retry backoff. Each
                # released tick hands control to the real lease heartbeat at
                # the advanced injected clock; no wall-clock wait is involved.
                if heartbeat.waits.empty():
                    # The unfixed composition has no heartbeat: reproduce its
                    # expiry directly rather than waiting for a timer.
                    clock["now"] += timedelta(seconds=331)
                else:
                    for _ in range(4):
                        tick = await heartbeat.waits.get()
                        while tick.cancelled():
                            tick = await heartbeat.waits.get()
                        clock["now"] += timedelta(seconds=100)
                        tick.set_result(None)
                    tick = await heartbeat.waits.get()  # last renewal completed
                    while tick.cancelled():
                        tick = await heartbeat.waits.get()
                    heartbeat.waits.put_nowait(tick)
                assert not await accounting.acquire_lease(
                    ctx.tenant_id, uuid.uuid4(), max_concurrency=1, ttl_seconds=60
                ), "Live unconfirmed cleanup must retain its slot beyond the original TTL"
                assert await accounting.usage(ctx.tenant_id) == (1, 30)
                assert accounting.settlements == 0
                assert local.is_active(stream_id)
                assert await backplane.get_terminal(stream_id) is None
                async with ctx.sessionmaker() as check:
                    run = (await check.scalars(select(models.CodeRun))).one()
                    assert run.status == CodeRunStatus.RUNNING.value
                    attempts = list(
                        await check.scalars(
                            select(models.AuditEvent).where(
                                models.AuditEvent.action == "code_run.cancelled"
                            )
                        )
                    )
                    # R3-002 on the chat Stop path: only the first failed
                    # attempt writes an error audit, however many retries the
                    # unconfirmed kill takes; later attempts are logged only.
                    assert len(attempts) == 1
                    assert all(event.outcome == "error" for event in attempts)
                    # The retry wait backs off exponentially from 1s (recorded
                    # injectable sleep, no wall-clock), never a fixed cadence.
                    assert retry_delays == [1.0 * 2**i for i in range(_attempt + 1)]
                    assert all(event.event_metadata.get("reason_code") for event in attempts)
                    assert "Synthetic cleanup" not in str(
                        [event.event_metadata for event in attempts]
                    )
                    for table in (models.Message, models.Citation, models.Artifact):
                        assert list(await check.scalars(select(table))) == []
                    assert not list(
                        await check.scalars(
                            select(models.AuditEvent).where(
                                models.AuditEvent.action == "answer.cancelled"
                            )
                        )
                    )
                timeout.reschedule(asyncio.get_running_loop().time() - 1)
                with pytest.raises(DependencyError) as error:
                    await asyncio.wait_for(asyncio.shield(request), 30)
                assert error.value.status == 503
                assert "could not be confirmed" in str(error.value.detail)
                release.set_result(None)

            request = asyncio.create_task(
                service.cancel(stream_id=stream_id, principal=ctx.principal)
            )
            requests.append(request)
            terminal = await asyncio.wait_for(asyncio.shield(request), 30)
            assert terminal["type"] == "cancelled"
            assert await asyncio.wait_for(asyncio.shield(producer), 30) is False
            assert not runner.remote_running
            assert (await accounting.usage(ctx.tenant_id))[0] == 0
            assert accounting.settlements == 1
            assert len(runner.executions) == 1
            assert len(runner.kills) == failures + 1
            assert all(kill == runner.kills[0] for kill in runner.kills)
            assert runner.kills[0][1] == runner.executions[0]
            assert gateway.calls == 1
            assert await service.cancel(stream_id=stream_id, principal=ctx.principal) == terminal
            assert len(runner.kills) == failures + 1
            events = backplane._replay[stream_id]
            assert [event["type"] for event in events if is_terminal(event)] == ["cancelled"]
            assert not any(event.get("name") in {"code_result", "tool_result"} for event in events)
            await _assert_cancelled_database(ctx, stream_id, partial="")
            async with ctx.sessionmaker() as check:
                run = (await check.scalars(select(models.CodeRun))).one()
                assert run.status == CodeRunStatus.KILLED.value
                attempts = list(
                    await check.scalars(
                        select(models.AuditEvent).where(
                            models.AuditEvent.action == "code_run.cancelled"
                        )
                    )
                )
                assert sum(event.outcome == "allowed" for event in attempts) == 1
                # First-failure-only error audit (R3-002), not one per attempt.
                assert sum(event.outcome == "error" for event in attempts) == 1
                assert retry_delays == [1.0 * 2**i for i in range(failures)]
        finally:
            tearing_down = True
            for barrier in barriers:
                if not barrier.done():
                    barrier.set_result(None)
            if pending_retry is not None:
                pending_retry.cancel()
            for request in requests:
                request.cancel()
            producer.cancel()
            # A second cancellation is the force-shutdown escape for parked cleanup.
            producer.cancel()
            await asyncio.gather(
                producer,
                *requests,
                *([pending_retry] if pending_retry is not None else []),
                return_exceptions=True,
            )
