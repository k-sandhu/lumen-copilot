"""The chat ``run_python`` submission seam — the runtime wiring (issue #231).

Drives :class:`app.sandbox.tool_runner.ChatSandboxToolRunner` **offline** against an
in-memory SQLite database, a **fake object store** (round-trips artifact bytes), a
**fake sandbox runner** (returns a scripted :class:`RunResult` or raises), and an
in-memory backplane that captures the published WS envelopes — so no container engine
/ MinIO / Redis is needed. It proves the seam *around* the merged #230 service:

* a submission **creates + links** a ``code_run`` to the parent chat session + the
  assistant message (the trace), drives the sandbox service to a terminal, and
  returns the :class:`~app.services.tools.types.SandboxRun` summary (exit status +
  artifact ids + run id);
* it **streams** the captured stdout/stderr as ``code_output`` chunks and terminates
  with exactly one ``code_result`` on the same chat stream (the frozen ADR-0013 WS
  contract), correlated by the ``runId``;
* a **disabled tenant** run is refused by the composed service (``status=denied``)
  and comes back as a ``denied`` :class:`SandboxRun` (never an exception) — the tool's
  ``ok=False`` negative;
* a produced file is captured as a tenant-scoped artifact, whose id rides the summary
  and the ``code_result``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.repositories import (
    ChatSessionRepository,
    CodeRunRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    UserRepository,
)
from app.domain.code_execution import (
    SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
    SANDBOX_REASON_DEPLOY_DISABLED,
)
from app.domain.entities import CodeRunStatus, ResourceUsage, Role
from app.realtime.backplane import InMemoryBackplane
from app.sandbox.accounting import AccountingUnavailable, InMemorySandboxAccounting
from app.sandbox.runner import SandboxRunnerUnavailable
from app.sandbox.spec import OutputFile, RunResult, RunSpec, SandboxSessionSpec
from app.sandbox.tool_runner import ChatSandboxToolRunner
from app.services.tools.types import ToolCancellationPending
from app.storage.keys import build_artifact_key
from tests._db_helpers import copy_sqlite_schema
from tests._sandbox_helpers import sandbox_settings
from tests.test_sandbox_pending_cleanup import CountingAccounting, RecordingSleep

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


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
    """A scripted sandbox runner: returns a result or raises (never a real container)."""

    def __init__(self, *, result: RunResult | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls = 0

    async def ensure_session(self, value: SandboxSessionSpec) -> None:
        return None

    async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result

    async def reset_session(
        self, previous: SandboxSessionSpec, replacement: SandboxSessionSpec
    ) -> None:
        return None

    async def close_session(self, value: SandboxSessionSpec) -> None:
        return None

    async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
        return None


def _ok_result(
    *, stdout: str = "hello", stderr: str = "", output_files: tuple[OutputFile, ...] = ()
) -> RunResult:
    return RunResult(
        status=CodeRunStatus.SUCCEEDED,
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        duration_ms=7,
        output_files=output_files,
        image_digest="sha256:deadbeef",
        resource_usage=ResourceUsage(peak_memory_bytes=2048, output_bytes=len(stdout)),
    )


# --- Fixtures ---------------------------------------------------------------


class _World:
    def __init__(
        self,
        *,
        session: AsyncSession,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        chat_id: uuid.UUID,
    ) -> None:
        self.session = session
        self.sessionmaker = sessionmaker
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.chat_id = chat_id

    def seam(
        self,
        runner: Any,
        *,
        backplane: InMemoryBackplane,
        store: _FakeStore | None = None,
        stream_id: str = "stream-1",
        session_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        settings_overrides: dict[str, object] | None = None,
        accounting: InMemorySandboxAccounting | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> ChatSandboxToolRunner:
        counter = {"seq": 0}

        def _next_seq() -> int:
            s = counter["seq"]
            counter["seq"] += 1
            return s

        return ChatSandboxToolRunner(
            sessionmaker=self.sessionmaker,
            tenant_id=self.tenant_id,
            owner_id=self.user_id,
            runner=runner,  # type: ignore[arg-type]  # structural fake
            object_store=(store or _FakeStore()),  # type: ignore[arg-type]  # structural fake
            settings=sandbox_settings(**(settings_overrides or {})),
            backplane=backplane,
            stream_id=stream_id,
            request_id="req-test",
            source_ip="203.0.113.5",
            next_seq=_next_seq,
            session_id=session_id or self.chat_id,
            message_id=message_id,
            accounting=accounting or InMemorySandboxAccounting(),
            retry_sleep=retry_sleep,
            lease_sleep=lease_sleep,
        )


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Acme")
            user = await UserRepository(session, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            # Enable the sandbox for this tenant (#233): deny-by-default means no policy
            # row would refuse EVERY run. The disabled-tenant negative overrides the
            # deploy-wide kill-switch (SANDBOX_ENABLED=false) which the AND still denies.
            await TenantSandboxPolicyRepository(session, tenant.id).upsert(
                enabled=True,
                allowed_packages=(),
                denied_packages=(),
                egress_allowed=False,
                egress_allowlist=(),
                max_runtime_s=30,
                max_memory_mb=512,
                daily_runtime_cap_s=3600,
                max_concurrency=2,
                updated_by=None,
            )
            chat = await ChatSessionRepository(session, tenant.id).create(
                owner_id=user.id, model="m", title="default"
            )
            await session.commit()
            yield _World(
                session=session,
                sessionmaker=factory,
                tenant_id=tenant.id,
                user_id=user.id,
                chat_id=chat.id,
            )
    finally:
        await engine.dispose()


def _events(backplane: InMemoryBackplane, stream_id: str) -> list[dict[str, Any]]:
    # The InMemoryBackplane buffers every published envelope for replay.
    return list(backplane._replay.get(stream_id, []))  # noqa: SLF001 — test inspection


# ---------------------------------------------------------------------------
# AC-1 — submit runs to a terminal, links the run, returns the summary.
# ---------------------------------------------------------------------------


async def test_submit_runs_captures_and_returns_summary(world: _World) -> None:
    backplane = InMemoryBackplane()
    output = OutputFile(filename="chart.png", content_type="image/png", data=b"\x89PNGdata")
    runner = _FakeRunner(result=_ok_result(stdout="done\n", output_files=(output,)))
    message_id = uuid.uuid4()
    # A real chat session so the code_run's session_id FK resolves.
    chat = await ChatSessionRepository(world.session, world.tenant_id).create(
        owner_id=world.user_id, model="m", title="t"
    )
    seam = world.seam(runner, backplane=backplane, session_id=chat.id, message_id=message_id)

    summary = await seam.submit(code="print('done')")

    assert summary.status is CodeRunStatus.SUCCEEDED
    assert summary.exit_code == 0
    assert summary.stdout == "done\n"
    assert len(summary.artifact_ids) == 1
    # The code_run was persisted, linked to the parent session + the trace (message).
    run = await CodeRunRepository(world.session, world.tenant_id).get(summary.code_run_id)
    assert run is not None
    assert run.session_id == chat.id
    assert run.trace_id == message_id
    assert run.owner_id == world.user_id  # runs on the streaming user's behalf (INV-2)
    assert [str(a) for a in run.artifact_ids] == [str(a) for a in summary.artifact_ids]


# ---------------------------------------------------------------------------
# WS contract — code_output chunks then exactly one code_result (ADR-0013).
# ---------------------------------------------------------------------------


async def test_streams_code_output_then_one_code_result(world: _World) -> None:
    backplane = InMemoryBackplane()
    runner = _FakeRunner(result=_ok_result(stdout="line1\n", stderr="warn\n"))
    seam = world.seam(runner, backplane=backplane, stream_id="s-1")

    summary = await seam.submit(code="x=1")

    events = _events(backplane, "s-1")
    outputs = [e for e in events if e.get("name") == "code_output"]
    results = [e for e in events if e.get("name") == "code_result"]
    # Both captured streams surfaced as code_output chunks, correlated by runId.
    assert {e["data"]["stream"] for e in outputs} == {"stdout", "stderr"}
    assert all(e["data"]["runId"] == str(summary.code_run_id) for e in outputs)
    # Exactly ONE terminal code_result, after the chunks, carrying the outcome.
    assert len(results) == 1
    result_event = results[0]
    assert result_event["data"]["runId"] == str(summary.code_run_id)
    assert result_event["data"]["status"] == "succeeded"
    assert result_event["data"]["artifactIds"] == []
    # Monotonic seq: the code_result is the last-emitted of the run's events.
    assert result_event["seq"] == max(e["seq"] for e in outputs + results)


# ---------------------------------------------------------------------------
# AC-2 (negative) — a disabled tenant is denied by the composed service.
# ---------------------------------------------------------------------------


async def test_disabled_tenant_is_denied_without_calling_runner(world: _World) -> None:
    backplane = InMemoryBackplane()
    runner = _FakeRunner(result=_ok_result())
    seam = world.seam(runner, backplane=backplane, settings_overrides={"SANDBOX_ENABLED": "false"})

    summary = await seam.submit(code="print(1)")

    # Refused before execution: the runner was NEVER called, the run is denied.
    assert summary.status is CodeRunStatus.DENIED
    assert runner.calls == 0
    # …and the seam carries the TYPED reason out, not just the refusal's prose
    # (issue #502): ``reason_code`` is not a ``code_runs`` column, so it can only
    # come from the execution outcome. This is what puts the stable code in the
    # durable ``tool_invocations`` row instead of a truncated sentence.
    assert summary.reason_code == SANDBOX_REASON_DEPLOY_DISABLED
    # A denied run emits a code_result (terminal) but no process code_output.
    events = _events(backplane, "stream-1")
    assert [e for e in events if e.get("name") == "code_output"] == []
    results = [e for e in events if e.get("name") == "code_result"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "denied"


class _BarrierSleep:
    def __init__(self) -> None:
        self.waits: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()

    async def __call__(self, _delay: float) -> None:
        release = asyncio.get_running_loop().create_future()
        self.waits.put_nowait(release)
        await release


async def test_accounting_loss_during_execution_returns_killed_summary(world: _World) -> None:
    """R3-001: renewal loss kills only the in-flight call, not the chat turn."""
    entered = asyncio.Event()
    heartbeat = _BarrierSleep()

    class Accounting(InMemorySandboxAccounting):
        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            raise AccountingUnavailable("accounting down")

    class Runner(_FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.kills: list[uuid.UUID] = []
            self.remote_running = True

        async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
            self.kills.append(execution_id)
            self.remote_running = False

    backplane = InMemoryBackplane()
    runner = Runner()
    seam = world.seam(
        runner,
        backplane=backplane,
        accounting=Accounting(),
        lease_sleep=heartbeat,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    try:
        await entered.wait()
        (await heartbeat.waits.get()).set_result(None)
        summary = await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert summary.status is CodeRunStatus.KILLED
    assert summary.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
    assert runner.kills == [summary.code_run_id]
    assert not runner.remote_running
    run = await CodeRunRepository(world.session, world.tenant_id).get(summary.code_run_id)
    assert run is not None
    assert run.status is CodeRunStatus.KILLED
    assert run.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
    results = [e for e in _events(backplane, "stream-1") if e.get("name") == "code_result"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "killed"


async def _audits(world: _World, action: str, outcome: str) -> list[models.AuditEvent]:
    return list(
        await world.session.scalars(
            select(models.AuditEvent).where(
                models.AuditEvent.action == action,
                models.AuditEvent.outcome == outcome,
            )
        )
    )


async def test_renewal_failure_after_execution_finalises_via_chat_seam(
    world: _World,
) -> None:
    """TF2/R3-001 under #830: post-execution renewal loss cannot cancel the turn.

    The run parks in output capture after ``runner.execute`` has returned, so
    a failing heartbeat tick has no in-flight call to cancel. Finalisation is
    normal: succeeded summary, one ``code_result``, one ``code_run.finished``
    audit, no cancellation audit, and admission settled/released once.
    """
    heartbeat = _BarrierSleep()

    class Accounting(CountingAccounting):
        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            raise AccountingUnavailable("accounting down")

    class ParkingStore(_FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release: asyncio.Future[None] | None = None

        async def put_artifact(
            self, tenant_id: str, data: bytes, content_type: str, filename: str
        ) -> object:
            self.release = asyncio.get_running_loop().create_future()
            self.entered.set()
            await self.release
            return await super().put_artifact(tenant_id, data, content_type, filename)

    accounting = Accounting()
    store = ParkingStore()
    backplane = InMemoryBackplane()
    runner = _FakeRunner(
        result=_ok_result(
            stdout="done\n",
            output_files=(OutputFile(filename="c.png", content_type="image/png", data=b"x"),),
        )
    )
    seam = world.seam(
        runner, backplane=backplane, store=store, accounting=accounting, lease_sleep=heartbeat
    )
    task = asyncio.create_task(seam.submit(code="print('done')"))
    try:
        await store.entered.wait()
        (await heartbeat.waits.get()).set_result(None)
        # The next wait proves the failed-renewal tick was fully handled
        # while the run was parked in capture.
        await heartbeat.waits.get()
        assert store.release is not None
        if not store.release.done():
            store.release.set_result(None)
        summary = await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not task.cancelled()
    assert summary.status is CodeRunStatus.SUCCEEDED
    run = await CodeRunRepository(world.session, world.tenant_id).get(summary.code_run_id)
    assert run is not None
    assert run.status is CodeRunStatus.SUCCEEDED
    assert run.termination_reason == "completed"
    assert len(await _audits(world, "code_run.finished", "allowed")) == 1
    assert await _audits(world, "code_run.cancelled", "allowed") == []
    assert await _audits(world, "code_run.cancelled", "error") == []
    results = [e for e in _events(backplane, "stream-1") if e.get("name") == "code_result"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "succeeded"
    assert accounting.settlements == accounting.releases == 1


async def test_accounting_loss_kill_retries_without_becoming_user_cancellation(
    world: _World,
) -> None:
    """TF2: an accounting-loss kill under #830 never becomes a user Stop.

    The first confirming kill fails; pending cleanup retries the same
    identity with recorded backoff and returns a killed accounting outcome.
    The chat Stop callback is not used: no allowed ``code_run.cancelled``,
    and the terminal audit is ``code_run.finished``/error carrying the
    accounting reason code.
    """
    entered = asyncio.Event()
    heartbeat = _BarrierSleep()
    retry = RecordingSleep()

    class Accounting(CountingAccounting):
        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            raise AccountingUnavailable("accounting down")

    class Runner(_FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.kills: list[uuid.UUID] = []

        async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
            self.kills.append(execution_id)
            if len(self.kills) == 1:
                raise SandboxRunnerUnavailable("unconfirmed")

    accounting = Accounting()
    runner = Runner()
    backplane = InMemoryBackplane()
    seam = world.seam(
        runner,
        backplane=backplane,
        accounting=accounting,
        lease_sleep=heartbeat,
        retry_sleep=retry,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    try:
        await entered.wait()
        (await heartbeat.waits.get()).set_result(None)
        summary = await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not task.cancelled()
    assert summary.status is CodeRunStatus.KILLED
    assert summary.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
    assert runner.kills == [summary.code_run_id, summary.code_run_id]
    assert retry.delays == [1.0]
    run = await CodeRunRepository(world.session, world.tenant_id).get(summary.code_run_id)
    assert run is not None
    assert run.status is CodeRunStatus.KILLED
    assert run.termination_reason == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
    errored = await _audits(world, "code_run.finished", "error")
    assert len(errored) == 1
    assert errored[0].event_metadata.get("reason_code") == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
    assert await _audits(world, "code_run.cancelled", "allowed") == []
    # Only the first unconfirmed attempt wrote an error audit (R3-002).
    assert len(await _audits(world, "code_run.cancelled", "error")) == 1
    results = [e for e in _events(backplane, "stream-1") if e.get("name") == "code_result"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "killed"
    assert accounting.settlements == accounting.releases == 1


async def test_chat_stop_cleanup_audits_first_failure_only(world: _World) -> None:
    """TF2/R3-002: user Stop kill retries through ``_cancel_run`` flood-guard.

    Cancelling the submission (Stop) defers to producer-owned pending
    cleanup. Three kill attempts fail, the fourth confirms: exactly one
    error audit (the first failure), one allowed ``code_run.cancelled``, a
    ``cancelled`` terminal (a Stop never claims the accounting reason), and
    admission settled/released once.
    """
    entered = asyncio.Event()
    heartbeat = _BarrierSleep()
    accounting = CountingAccounting()

    class Runner(_FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.kills: list[uuid.UUID] = []

        async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
            self.kills.append(execution_id)
            if len(self.kills) <= 3:
                raise SandboxRunnerUnavailable("unconfirmed")

    runner = Runner()
    seam = world.seam(
        runner, backplane=InMemoryBackplane(), accounting=accounting, lease_sleep=heartbeat
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    await entered.wait()
    task.cancel()
    with pytest.raises(ToolCancellationPending) as pending_info:
        await task
    pending = pending_info.value
    for _ in range(2):
        with pytest.raises(SandboxRunnerUnavailable):
            await pending.cleanup()
    await pending.cleanup()
    assert len(runner.kills) == 4
    assert len(set(runner.kills)) == 1
    run = await CodeRunRepository(world.session, world.tenant_id).get(runner.kills[0])
    assert run is not None
    assert run.status is CodeRunStatus.KILLED
    assert run.termination_reason == "cancelled"
    assert len(await _audits(world, "code_run.cancelled", "error")) == 1
    assert len(await _audits(world, "code_run.cancelled", "allowed")) == 1
    assert accounting.settlements == accounting.releases == 1
    assert (await accounting.usage(world.tenant_id))[0] == 0


async def test_runner_crash_becomes_failed_terminal_not_exception(world: _World) -> None:
    backplane = InMemoryBackplane()
    runner = _FakeRunner(raises=RuntimeError("runner exploded"))
    seam = world.seam(runner, backplane=backplane)

    # The composed service catches the crash and writes ``failed`` — the seam never
    # raises, so the tool always gets a terminal record back.
    summary = await seam.submit(code="boom")

    assert summary.status is CodeRunStatus.FAILED
    results = [e for e in _events(backplane, "stream-1") if e.get("name") == "code_result"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "failed"


async def test_chat_stop_cancels_active_sandbox_without_late_output(world: _World) -> None:
    import asyncio

    entered = asyncio.Event()
    cancelled: list[uuid.UUID] = []

    class BlockingRunner(_FakeRunner):
        async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def cancel(self, value: SandboxSessionSpec, execution_id: uuid.UUID) -> None:
            cancelled.append(execution_id)

    backplane = InMemoryBackplane()
    seam = world.seam(BlockingRunner(), backplane=backplane)
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(cancelled) == 1
    async with world.sessionmaker() as session:
        run = await CodeRunRepository(session, world.tenant_id).get(cancelled[0])
        assert run is not None and run.status is CodeRunStatus.KILLED
    assert not backplane._replay.get("stream-1")
