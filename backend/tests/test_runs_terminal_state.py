"""Run terminal-state regressions (#578) — a claimed run always ends honestly.

The seam the older ``test_runs_service`` suite missed: its SQLite engine leaves
foreign keys OFF, so linking a run to an assistant message that was rolled
back (the failure path) silently succeeds there while Postgres raises an
``IntegrityError`` and strands the run in ``running`` (the 2026-10-15 repro).
This file runs with ``PRAGMA foreign_keys=ON`` and a fake runtime that
publishes scripted envelopes straight into the :class:`RunTranscriptSink`,
so each terminal path is deterministic:

* an ``error`` terminal whose ``start`` message id was never persisted still
  lands ``failed`` (never stuck ``running``), with ``message_id`` unset;
* a denied tool result (``code_execution_denied``) + ``done`` lands
  ``failed``/``tool_failure`` with the failed call visible in the steps;
* a recoverable tool error + ``done`` still lands ``succeeded``;
* a Phase-3 persistence failure falls back to a fresh-transaction terminal;
* ``mark_running`` claims exactly once (atomic ``WHERE status='queued'``);
* the stale-run reaper fails an abandoned ``running`` run exactly once and
  never overwrites a concurrent finish (INV-1: cross-tenant is a no-op);
* the Celery wrapper leaves a terminal state when ``execute_run`` raises
  after the claim, idempotently on redelivery.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db import models
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    AuditEventRepository,
    MessageRepository,
    RunRepository,
    RunStepRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    KnowledgeScope,
    MessageRole,
    Role,
    RunError,
    RunStatus,
    RunStepKind,
    RunTrigger,
)
from app.realtime import envelopes
from app.services import runs_service
from app.services.assistants_service import config_from_assistant
from tests._audit_helpers import RecordingDurableAuditTransactions
from tests._db_helpers import copy_sqlite_schema

import app.db.models  # noqa: F401  isort: skip


# --- Fake runtime: scripted envelopes into the sink -------------------------


class _FakeRuntime:
    """Publishes a scripted envelope stream; optionally persists the message first.

    Mirrors the real runtime's contract: the message id rides ``start`` before
    the row exists; the row is written (and committed) only on the path that
    produces ``done``. On the ``error`` path nothing is persisted, so the run
    must not link the dangling id.
    """

    def __init__(self, script: str, **kwargs: Any) -> None:
        self._script = script
        self._sink = kwargs["backplane"]
        self._sessionmaker = kwargs["sessionmaker"]
        self._principal = kwargs["principal"]
        self.message_id = uuid.uuid4()

    async def run(self, *, stream_id: str, session_id: uuid.UUID, **_: Any) -> None:
        publish = self._sink.publish
        await publish(
            stream_id,
            envelopes.start(stream_id, 0, data={"messageId": str(self.message_id)}),
        )
        if self._script in {"error", "transient"}:
            await publish(
                stream_id,
                envelopes.error(
                    stream_id,
                    1,
                    {
                        "code": "dependency_unavailable"
                        if self._script == "transient"
                        else "internal_error",
                        "detail": "The run failed.",
                    },
                ),
            )
            return
        if self._script == "denied_tool":
            await publish(
                stream_id,
                envelopes.event(
                    stream_id,
                    1,
                    name="tool_result",
                    data={
                        "callId": "c1",
                        "tool": "run_python",
                        "ok": False,
                        "error": "code_execution_denied",
                        "summary": "code_execution_denied",
                    },
                ),
            )
        elif self._script == "recoverable_tool":
            await publish(
                stream_id,
                envelopes.event(
                    stream_id,
                    1,
                    name="tool_result",
                    data={
                        "callId": "c1",
                        "tool": "search_text",
                        "ok": False,
                        "error": "tool_bad_args",
                        "summary": "tool_bad_args",
                    },
                ),
            )
        # The succeeded path: the assistant message is persisted before ``done``.
        if self._script == "missing_message":
            await publish(stream_id, envelopes.done(stream_id, 3, data={"citationCount": 0}))
            return
        async with self._sessionmaker() as session:
            await MessageRepository(session, self._principal.tenant_id).add_with_id(
                message_id=self.message_id,
                session_id=session_id,
                role=MessageRole.ASSISTANT,
                content="The weekly report is ready.",
                model="m",
            )
            await session.commit()
        await publish(stream_id, envelopes.delta(stream_id, 2, {"text": "The weekly report."}))
        await publish(stream_id, envelopes.done(stream_id, 3, data={"citationCount": 0}))


def _patch_fake_runtime(monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    monkeypatch.setattr(
        runs_service, "ChatRuntime", lambda **kwargs: _FakeRuntime(script, **kwargs)
    )


# --- Fixture: FK-ON SQLite + seeded tenant/assistant ------------------------


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice_id: uuid.UUID,
        assistant_id: uuid.UUID,
        version_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_id = alice_id
        self.assistant_id = assistant_id
        self.version_id = version_id


@pytest_asyncio.fixture
async def ctx(
    monkeypatch: pytest.MonkeyPatch,
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(connection: object, _record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        info={"durable_audit_ledger": durable_audit_ledger},
    )
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda settings=None: factory)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        async with factory() as seed:
            ta = await TenantRepository(seed).create(name="Acme")
            tb = await TenantRepository(seed).create(name="Globex")
            alice = await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            assistants = AssistantRepository(seed, ta.id)
            assistant = await assistants.create(
                owner_id=alice.id,
                name="Weekly summary",
                instructions="Summarize.",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=bob.id,
            )
            await assistants.update(assistant.id, fields={"status": AssistantStatus.PUBLISHED})
            published = await assistants.get(assistant.id)
            version = await AssistantVersionRepository(seed, ta.id).add(
                assistant_id=assistant.id,
                version=1,
                author_id=alice.id,
                config=config_from_assistant(published),
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice_id=alice.id,
                assistant_id=assistant.id,
                version_id=version.id,
            )
    finally:
        await engine.dispose()


async def _queued_run(ctx: _Ctx, *, tenant_id: uuid.UUID | None = None) -> uuid.UUID:
    tenant = tenant_id or ctx.tenant_a
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, tenant).create(
            owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_id,
            assistant_version_id=ctx.version_id,
            trigger=RunTrigger.MANUAL,
        )
        await session.commit()
        return run.id


async def _set_running(
    ctx: _Ctx, run_id: uuid.UUID, *, started_at: datetime, tenant_id: uuid.UUID | None = None
) -> None:
    async with ctx.sessionmaker() as session:
        await session.execute(
            update(models.Run)
            .where(models.Run.id == run_id)
            .values(status=RunStatus.RUNNING.value, started_at=started_at)
        )
        await session.commit()


async def _finished_audit_count(ctx: _Ctx, run_id: uuid.UUID) -> int:
    async with ctx.sessionmaker() as session:
        recent = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=200)
    return sum(1 for e in recent if e.resource_id == str(run_id) and e.action == "run.finished")


async def _delivery_count(ctx: _Ctx, run_id: uuid.UUID) -> int:
    async with ctx.sessionmaker() as session:
        stmt = (
            select(func.count())
            .select_from(models.RunDelivery)
            .where(models.RunDelivery.run_id == run_id)
        )
        return int((await session.execute(stmt)).scalar_one())


# --- The stuck-Running repro: error terminal, message never persisted -------


async def test_error_terminal_with_unpersisted_message_still_fails_run(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With FKs on, a failed run must not link the rolled-back ``start`` message.

    Before the fix this raised ``IntegrityError`` out of Phase 3 and the run
    row stayed ``running`` with no steps and no ``finished_at`` (2026-10-15
    repro, #578).
    """
    _patch_fake_runtime(monkeypatch, "error")
    run_id = await _queued_run(ctx)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert run.finished_at is not None
        assert run.message_id is None  # the start-envelope id was never persisted
        assert run.error is not None and run.error.code == "internal_error"
        steps = await RunStepRepository(session, ctx.tenant_a).list_for_run(run_id)
        assert [s.kind for s in steps] == [RunStepKind.ERROR]
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1  # a failed run still reaches the owner


async def test_denied_tool_run_fails_with_tool_failure_and_visible_steps(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``code_execution_denied`` + ``done`` is Failed/tool_failure, steps intact."""
    _patch_fake_runtime(monkeypatch, "denied_tool")
    run_id = await _queued_run(ctx)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.finished_at is not None
        assert run.error is not None and run.error.code == "tool_failure"
        # Only a successful run links its completed answer; failed calls remain steps.
        assert run.message_id is None
        steps = await RunStepRepository(session, ctx.tenant_a).list_for_run(run_id)
        tool_results = [s for s in steps if s.kind is RunStepKind.TOOL_RESULT]
        assert len(tool_results) == 1
        assert tool_results[0].payload.get("ok") is False
        assert tool_results[0].payload.get("error") == "code_execution_denied"


async def test_done_without_persisted_response_fails_honestly(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_fake_runtime(monkeypatch, "missing_message")
    run_id = await _queued_run(ctx)
    assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.finished_at is not None
        assert run.message_id is None
        assert run.error is not None and run.error.code == "internal_error"
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1


async def test_recoverable_tool_error_run_succeeds_and_links_message(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad-args error the model recovered from still lands Succeeded."""
    _patch_fake_runtime(monkeypatch, "recoverable_tool")
    run_id = await _queued_run(ctx)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.SUCCEEDED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.error is None
        assert run.message_id is not None
        assert run.summary and "weekly report" in run.summary
        results = [
            s
            for s in await RunStepRepository(session, ctx.tenant_a).list_for_run(run_id)
            if s.kind is RunStepKind.TOOL_RESULT
        ]
        assert len(results) == 1
        assert results[0].payload["ok"] is False
        assert results[0].payload["error"] == "tool_bad_args"


async def test_phase3_failure_falls_back_to_fresh_transaction_terminal(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If persisting the outcome blows up, a fresh txn still terminal-fails the run."""
    _patch_fake_runtime(monkeypatch, "clean")

    async def _boom_persist(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private vendor text must not reach the run")

    monkeypatch.setattr(runs_service.RunTranscriptSink, "persist", _boom_persist)
    run_id = await _queued_run(ctx)

    status = await runs_service.execute_run(run_id, ctx.tenant_a)

    assert status is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED  # never stuck running
        assert run.finished_at is not None
        assert run.error is not None and run.error.code == "internal_error"
        assert "private vendor" not in run.error.message
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1


# --- Atomic claim -----------------------------------------------------------


async def test_mark_running_claims_exactly_once(ctx: _Ctx) -> None:
    """The claim is a CAS on ``status='queued'``: the second claimant loses."""
    run_id = await _queued_run(ctx)
    started = datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
    async with ctx.sessionmaker() as session:
        runs = RunRepository(session, ctx.tenant_a)
        first = await runs.mark_running(run_id, started_at=started)
        assert first is not None and first.status is RunStatus.RUNNING
        second = await runs.mark_running(run_id, started_at=started + timedelta(minutes=5))
        assert second is None  # already claimed — exactly one winner
        await session.commit()
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.started_at is not None
        assert run.started_at.replace(tzinfo=UTC) == started  # first claim's stamp kept


async def test_mark_running_cross_tenant_claims_nothing(ctx: _Ctx) -> None:
    """INV-1: a foreign tenant's repository cannot claim (or disturb) the run."""
    run_id = await _queued_run(ctx)
    async with ctx.sessionmaker() as session:
        claimed = await RunRepository(session, ctx.tenant_b).mark_running(
            run_id, started_at=datetime.now(UTC)
        )
        assert claimed is None
        await session.commit()
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.status is RunStatus.QUEUED


async def test_duplicate_delivery_while_runtime_is_active_creates_nothing(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedRuntime(_FakeRuntime):
        async def run(self, **kwargs: Any) -> None:
            entered.set()
            await release.wait()
            await super().run(**kwargs)

    monkeypatch.setattr(runs_service, "ChatRuntime", lambda **kw: PausedRuntime("clean", **kw))
    run_id = await _queued_run(ctx)
    first = asyncio.create_task(runs_service.execute_run(run_id, ctx.tenant_a))
    try:
        await entered.wait()
        assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.RUNNING
    finally:
        release.set()
        assert await first is RunStatus.SUCCEEDED
    assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.SUCCEEDED
    async with ctx.sessionmaker() as session:
        for table in (models.ChatSession, models.Message):
            assert (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one() == 1
        events = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=200)
        assert sum(e.action == "run.started" for e in events) == 1
        assert len(await RunStepRepository(session, ctx.tenant_a).list_for_run(run_id)) == 1
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1


@pytest.mark.parametrize("redelivery_attempt", [0, 1])
async def test_transient_retry_reuses_chat_session_and_does_not_redeliver(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, redelivery_attempt: int
) -> None:
    _patch_fake_runtime(monkeypatch, "transient")
    run_id = await _queued_run(ctx)
    with pytest.raises(runs_service.TransientRunRetry):
        await runs_service.execute_run(run_id, ctx.tenant_a, attempt=0)
    assert await _finished_audit_count(ctx, run_id) == 0
    assert await _delivery_count(ctx, run_id) == 0
    _patch_fake_runtime(monkeypatch, "clean")
    # A delayed copy of the original delivery still carries retries=0.
    assert (
        await runs_service.execute_run(run_id, ctx.tenant_a, attempt=redelivery_attempt)
        is RunStatus.SUCCEEDED
    )
    assert (
        await runs_service.execute_run(run_id, ctx.tenant_a, attempt=redelivery_attempt)
        is RunStatus.SUCCEEDED
    )
    async with ctx.sessionmaker() as session:
        assert (
            await session.execute(select(func.count()).select_from(models.ChatSession))
        ).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(models.Message))
        ).scalar_one() == 1
        events = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=200)
        assert sum(e.action == "run.started" for e in events) == 1
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1


async def test_claim_rejects_queue_snapshot_superseded_by_transient_attempt(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery paused after its read must not use stale execution identity."""
    run_id = await _queued_run(ctx)
    original = RunRepository.mark_running
    interleaved = False

    async def retry_before_claim(self: RunRepository, rid: uuid.UUID, **kwargs: Any) -> Any:
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            _patch_fake_runtime(monkeypatch, "transient")
            with pytest.raises(runs_service.TransientRunRetry):
                await runs_service.execute_run(run_id, ctx.tenant_a)
            _patch_fake_runtime(monkeypatch, "clean")
        return await original(self, rid, **kwargs)

    monkeypatch.setattr(RunRepository, "mark_running", retry_before_claim)
    assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.QUEUED
    assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.SUCCEEDED
    async with ctx.sessionmaker() as session:
        assert (
            await session.execute(select(func.count()).select_from(models.ChatSession))
        ).scalar_one() == 1
        events = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=200)
        assert sum(e.action == "run.started" for e in events) == 1
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1


async def test_retry_claim_identity_advances_with_a_frozen_clock(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return _NOW

    monkeypatch.setattr(runs_service, "datetime", FrozenClock)
    _patch_fake_runtime(monkeypatch, "transient")
    run_id = await _queued_run(ctx)
    with pytest.raises(runs_service.TransientRunRetry) as first:
        await runs_service.execute_run(run_id, ctx.tenant_a)
    with pytest.raises(runs_service.TransientRunRetry) as second:
        await runs_service.execute_run(run_id, ctx.tenant_a)
    assert second.value.started_at > first.value.started_at
    assert (
        await runs_service.ensure_run_terminal_on_failure(
            run_id,
            ctx.tenant_a,
            error=RunError(
                code="retry_publish_failed", message="The retry could not be scheduled."
            ),
            retry_started_at=first.value.started_at,
        )
        is RunStatus.QUEUED
    )
    assert await _finished_audit_count(ctx, run_id) == 0
    assert await _delivery_count(ctx, run_id) == 0


@pytest.mark.parametrize("control", ["resume", "reroute"])
async def test_owner_control_starts_fresh_execution_after_retry(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    from app.services.audit import AuditSink
    from tests._audit_helpers import denial_context_from_session

    _patch_fake_runtime(monkeypatch, "transient")
    run_id = await _queued_run(ctx)
    with pytest.raises(runs_service.TransientRunRetry):
        await runs_service.execute_run(run_id, ctx.tenant_a)
    async with ctx.sessionmaker() as session:
        runs = RunRepository(session, ctx.tenant_a)
        prior = await runs.get(run_id)
        assert prior is not None and prior.session_id is not None
        await runs.mark_terminal(run_id, status=RunStatus.ESCALATED, finished_at=_NOW)
        controls = runs_service.RunsControlService(
            session,
            tenant_id=ctx.tenant_a,
            owner_id=ctx.alice_id,
            audit=AuditSink(AuditEventRepository(session, ctx.tenant_a)),
            denials=denial_context_from_session(session, ctx.tenant_a, ctx.alice_id),
        )
        owner_id = ctx.alice_id
        if control == "reroute":
            owner_id = (
                await session.execute(
                    select(models.User.id).where(
                        models.User.tenant_id == ctx.tenant_a, models.User.id != ctx.alice_id
                    )
                )
            ).scalar_one()
            await controls.reroute(run_id, to_owner_id=owner_id)
        else:
            await controls.resume(run_id)
        await session.commit()
    _patch_fake_runtime(monkeypatch, "clean")
    assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.SUCCEEDED
    async with ctx.sessionmaker() as session:
        current = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert current is not None and current.session_id != prior.session_id
        chat = await session.get(models.ChatSession, current.session_id)
        assert chat is not None and chat.owner_id == owner_id
        events = await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=200)
        assert sum(e.action == "run.started" for e in events) == 2


async def test_abandoned_retry_handoff_is_reaped_but_never_claimed_queue_is_not(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker death after reset, before retry publication, leaves a durable lease."""
    _patch_fake_runtime(monkeypatch, "transient")
    run_id = await _queued_run(ctx)
    never_claimed = await _queued_run(ctx)
    with pytest.raises(runs_service.TransientRunRetry) as retry:
        await runs_service.execute_run(run_id, ctx.tenant_a)
    future = retry.value.started_at.replace(tzinfo=UTC) + timedelta(days=1)
    assert (await _reap(future)).reaped == 1
    async with ctx.sessionmaker() as session:
        runs = RunRepository(session, ctx.tenant_a)
        abandoned = await runs.get(run_id)
        assert abandoned is not None and abandoned.status is RunStatus.FAILED
        assert abandoned.finished_at is not None
        assert abandoned.error is not None and abandoned.error.code == "stale_run_timeout"
        untouched = await runs.get(never_claimed)
        assert untouched is not None and untouched.status is RunStatus.QUEUED
    assert (await _reap(future)).reaped == 0
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1


@pytest.mark.parametrize(
    "competitor", [None, "running", "queued", "finished", "recovery_unavailable"]
)
async def test_retry_publish_failure_recovers_only_its_queued_claim(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, competitor: str | None
) -> None:
    """Exercise the real retry -> publish failure -> Reject(requeue=False) path.

    The publish seam deterministically lets another claimant win before raising.
    A later attempt may already have reset to Queued again; status alone cannot
    fence the old handoff's recovery in that case.
    """
    from celery.exceptions import Reject
    from kombu.exceptions import OperationalError

    from app.tasks import run_assistant as task_module

    _patch_fake_runtime(monkeypatch, "transient")
    run_id = await _queued_run(ctx)
    monkeypatch.setattr(task_module, "run_task", lambda coro: asyncio.run(coro))
    if competitor == "recovery_unavailable":

        async def recovery_unavailable(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("private database diagnostics")

        monkeypatch.setattr(task_module, "ensure_run_terminal_on_failure", recovery_unavailable)

    async def compete() -> None:
        async with ctx.sessionmaker() as session:
            runs = RunRepository(session, ctx.tenant_a)
            prior = await runs.get(run_id)
            assert prior is not None
            # A fixed successor lease, without clocks, sleeps, or latency assertions.
            newer_start = (prior.started_at or _NOW) + timedelta(seconds=1)
            assert (
                await runs.mark_running(
                    run_id, started_at=newer_start, expected_started_at=prior.started_at
                )
                is not None
            )
            if competitor == "queued":
                # Simulate a second retry handoff using persisted state alone.
                await session.execute(
                    update(models.Run)
                    .where(models.Run.id == run_id)
                    .values(status=RunStatus.QUEUED.value)
                )
            elif competitor == "finished":
                await runs.mark_terminal(run_id, status=RunStatus.SUCCEEDED, finished_at=_NOW)
            await session.commit()

    def broker_down(*args: Any, **kwargs: Any) -> Any:
        if competitor in {"running", "queued", "finished"}:
            asyncio.run(compete())
        raise OperationalError("private broker diagnostics")

    monkeypatch.setattr(task_module.run_assistant, "apply_async", broker_down)

    def drive() -> None:
        task_module.run_assistant.push_request(
            id="retry-publication",
            called_directly=False,
            is_eager=False,
            retries=0,
            args=(str(run_id), str(ctx.tenant_a)),
            kwargs={},
            delivery_info={},
        )
        try:
            with pytest.raises(Reject) as rejection:
                task_module.run_assistant.run(str(run_id), str(ctx.tenant_a))
            assert rejection.value.requeue is False
        finally:
            task_module.run_assistant.pop_request()

    await asyncio.to_thread(drive)
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        if competitor is None:
            assert run.status is RunStatus.FAILED and run.finished_at is not None
            assert run.error is not None and run.error.code == "retry_publish_failed"
            assert "private" not in run.error.message
        else:
            expected = {
                "running": RunStatus.RUNNING,
                "queued": RunStatus.QUEUED,
                "finished": RunStatus.SUCCEEDED,
                "recovery_unavailable": RunStatus.QUEUED,
            }[competitor]
            assert run.status is expected
    assert await _finished_audit_count(ctx, run_id) == (1 if competitor is None else 0)
    assert await _delivery_count(ctx, run_id) == (1 if competitor is None else 0)
    if competitor == "recovery_unavailable":
        assert run.started_at is not None
        future = run.started_at.replace(tzinfo=UTC) + timedelta(days=1)
        assert (await _reap(future)).reaped == 1
        assert await _finished_audit_count(ctx, run_id) == 1
        assert await _delivery_count(ctx, run_id) == 1


async def test_successful_retry_publication_remains_queued_without_finishing(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from celery.exceptions import Retry

    from app.tasks import run_assistant as task_module

    _patch_fake_runtime(monkeypatch, "transient")
    run_id = await _queued_run(ctx)
    published: list[dict[str, Any]] = []
    monkeypatch.setattr(task_module, "run_task", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(
        task_module.run_assistant, "apply_async", lambda *args, **kwargs: published.append(kwargs)
    )

    def drive() -> None:
        task_module.run_assistant.push_request(
            id="retry-success",
            called_directly=False,
            is_eager=False,
            retries=0,
            args=(str(run_id), str(ctx.tenant_a)),
            kwargs={},
            delivery_info={},
        )
        try:
            with pytest.raises(Retry):
                task_module.run_assistant.run(str(run_id), str(ctx.tenant_a))
        finally:
            task_module.run_assistant.pop_request()

    await asyncio.to_thread(drive)
    assert len(published) == 1 and published[0]["retries"] == 1
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.status is RunStatus.QUEUED
        assert run.started_at is not None and run.finished_at is None
    assert await _finished_audit_count(ctx, run_id) == 0
    assert await _delivery_count(ctx, run_id) == 0


async def test_retry_reset_cannot_resurrect_a_reaped_run(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_fake_runtime(monkeypatch, "transient")
    original = RunRepository.mark_queued

    async def reaper_wins(self: RunRepository, run_id: uuid.UUID, **kwargs: Any) -> Any:
        async with ctx.sessionmaker() as other:
            assert await runs_service.reap_stale_run(
                other,
                tenant_id=ctx.tenant_a,
                run_id=run_id,
                older_than=datetime(2100, 1, 1, tzinfo=UTC),
                finished_at=datetime.now(UTC),
            )
            await other.commit()
        return await original(self, run_id, **kwargs)

    monkeypatch.setattr(RunRepository, "mark_queued", reaper_wins)
    run_id = await _queued_run(ctx)
    assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.FAILED
    assert await _finished_audit_count(ctx, run_id) == 1


async def test_terminal_cas_loses_to_reaper_without_steps_or_second_audit(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaper wins after Phase 3's read but before the terminal UPDATE."""
    _patch_fake_runtime(monkeypatch, "clean")
    original = RunRepository.mark_terminal

    async def reaper_wins(self: RunRepository, run_id: uuid.UUID, **kwargs: Any) -> Any:
        async with ctx.sessionmaker() as other:
            assert await runs_service.reap_stale_run(
                other,
                tenant_id=ctx.tenant_a,
                run_id=run_id,
                older_than=datetime(2100, 1, 1, tzinfo=UTC),
                finished_at=datetime.now(UTC),
            )
            await other.commit()
        return await original(self, run_id, **kwargs)

    monkeypatch.setattr(RunRepository, "mark_terminal", reaper_wins)
    run_id = await _queued_run(ctx)
    assert await runs_service.execute_run(run_id, ctx.tenant_a) is RunStatus.FAILED
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.error is not None
        assert run.error.code == "stale_run_timeout"
        assert await RunStepRepository(session, ctx.tenant_a).list_for_run(run_id) == []
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 1


async def test_failure_fallback_cannot_overwrite_concurrent_success(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = RunRepository.mark_terminal

    async def finish_first(self: RunRepository, run_id: uuid.UUID, **kwargs: Any) -> Any:
        async with ctx.sessionmaker() as other:
            await original(
                RunRepository(other, ctx.tenant_a),
                run_id,
                status=RunStatus.SUCCEEDED,
                finished_at=datetime.now(UTC),
                summary="done",
            )
            await other.commit()
        return await original(self, run_id, **kwargs)

    monkeypatch.setattr(RunRepository, "mark_terminal", finish_first)
    run_id = await _queued_run(ctx)
    await _set_running(ctx, run_id, started_at=datetime.now(UTC))
    assert (
        await runs_service.ensure_run_terminal_on_failure(
            run_id, ctx.tenant_a, error=RunError(code="internal_error", message="safe")
        )
        is RunStatus.SUCCEEDED
    )
    assert await _finished_audit_count(ctx, run_id) == 0  # losing fallback owns no event
    assert await _delivery_count(ctx, run_id) == 0


async def test_delivery_never_announces_a_nonterminal_run(ctx: _Ctx) -> None:
    from app.services.audit import AuditSink

    run_id = await _queued_run(ctx)
    for running in (False, True):
        if running:
            await _set_running(ctx, run_id, started_at=datetime.now(UTC))
        async with ctx.sessionmaker() as session:
            run = await RunRepository(session, ctx.tenant_a).get(run_id)
            assert run is not None
            assert (
                await runs_service.deliver_run(
                    session,
                    tenant_id=ctx.tenant_a,
                    run=run,
                    audit=AuditSink(AuditEventRepository(session, ctx.tenant_a)),
                )
                is None
            )
            await session.commit()
        assert await _delivery_count(ctx, run_id) == 0


@pytest.mark.parametrize("path", ["success", "reaper", "fallback"])
async def test_delivery_storage_failure_cannot_rollback_terminal(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.db.repositories import RunDeliveryRepository

    async def broken_delivery(self: Any, **kwargs: Any) -> Any:
        # A real failed flush poisons the current transaction unless isolated.
        self._session.add(
            models.RunStep(
                tenant_id=ctx.tenant_a, run_id=uuid.uuid4(), seq=100, kind="error", payload={}
            )
        )
        with pytest.raises(IntegrityError):
            await self._session.flush()
        raise RuntimeError("private delivery backend failure")

    monkeypatch.setattr(RunDeliveryRepository, "create", broken_delivery)
    _patch_fake_runtime(monkeypatch, "clean")
    run_id = await _queued_run(ctx)
    if path == "success":
        expected = RunStatus.SUCCEEDED
        assert await runs_service.execute_run(run_id, ctx.tenant_a) is expected
    else:
        expected = RunStatus.FAILED
        await _set_running(ctx, run_id, started_at=_NOW - timedelta(days=1))
        if path == "reaper":
            assert (await _reap(_NOW)).reaped == 1
        else:
            assert (
                await runs_service.ensure_run_terminal_on_failure(
                    run_id, ctx.tenant_a, error=RunError(code="internal_error", message="safe")
                )
                is expected
            )
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.status is expected and run.finished_at is not None
    assert await _finished_audit_count(ctx, run_id) == 1
    assert await _delivery_count(ctx, run_id) == 0


# --- Stale-run reaper ---------------------------------------------------------

_NOW = datetime(2026, 10, 15, 23, 0, tzinfo=UTC)


async def _reap(now: datetime) -> Any:
    from app.tasks.run_reaper import reap_stale_runs_async

    return await reap_stale_runs_async(now=now)


async def test_reaper_fails_stale_running_run_exactly_once(ctx: _Ctx) -> None:
    stale = await _queued_run(ctx)
    await _set_running(ctx, stale, started_at=_NOW - timedelta(days=1))
    fresh = await _queued_run(ctx)
    await _set_running(ctx, fresh, started_at=_NOW - timedelta(seconds=30))

    result = await _reap(_NOW)
    assert result.reaped == 1

    async with ctx.sessionmaker() as session:
        runs = RunRepository(session, ctx.tenant_a)
        reaped = await runs.get(stale)
        assert reaped is not None
        assert reaped.status is RunStatus.FAILED
        assert reaped.finished_at is not None
        assert reaped.error is not None and reaped.error.code  # a safe, typed reason
        assert "vendor" not in reaped.error.message.lower()
        untouched = await runs.get(fresh)
        assert untouched is not None and untouched.status is RunStatus.RUNNING
    assert await _finished_audit_count(ctx, stale) == 1
    assert await _delivery_count(ctx, stale) == 1

    # A second sweep at the same instant is a no-op for the reaped run
    # (exactly once; the fresh run has not crossed the cutoff either).
    second = await _reap(_NOW)
    assert second.reaped == 0
    assert await _finished_audit_count(ctx, stale) == 1
    assert await _delivery_count(ctx, stale) == 1


async def test_reaper_never_overwrites_a_concurrent_finish(ctx: _Ctx) -> None:
    """A run that finished after discovery keeps its terminal (CAS loses)."""
    run_id = await _queued_run(ctx)
    await _set_running(ctx, run_id, started_at=_NOW - timedelta(days=1))
    async with ctx.sessionmaker() as session:
        runs = RunRepository(session, ctx.tenant_a)
        finished = await runs.mark_terminal(
            run_id, status=RunStatus.SUCCEEDED, finished_at=_NOW, summary="done"
        )
        assert finished is not None
        cas = await runs.fail_stale_running(
            run_id,
            older_than=_NOW - timedelta(hours=1),
            finished_at=_NOW,
            error=RunError(code="stale_run_timeout", message="x"),  # ignored on a lost CAS
        )
        assert cas is None
        await session.commit()
    result = await _reap(_NOW + timedelta(hours=1))
    assert result.reaped == 0
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.status is RunStatus.SUCCEEDED


@pytest.mark.parametrize("queued_retry", [False, True])
async def test_fail_stale_running_cross_tenant_is_a_noop(ctx: _Ctx, queued_retry: bool) -> None:
    """INV-1: the reaper CAS is tenant-scoped — a foreign tenant changes nothing."""
    run_id = await _queued_run(ctx)
    await _set_running(ctx, run_id, started_at=_NOW - timedelta(days=1))
    async with ctx.sessionmaker() as session:
        if queued_retry:
            await RunRepository(session, ctx.tenant_a).mark_queued(run_id, preserve_claim=True)
        cas = await RunRepository(session, ctx.tenant_b).fail_stale_running(
            run_id,
            older_than=_NOW - timedelta(hours=1),
            finished_at=_NOW,
            error=RunError(code="stale_run_timeout", message="x"),
        )
        assert cas is None
        await session.commit()
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None
        assert run.status is (RunStatus.QUEUED if queued_retry else RunStatus.RUNNING)


async def test_reaper_cannot_overwrite_retry_claimed_after_discovery(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = await _queued_run(ctx)
    await _set_running(ctx, run_id, started_at=_NOW - timedelta(days=1))
    async with ctx.sessionmaker() as session:
        await RunRepository(session, ctx.tenant_a).mark_queued(run_id, preserve_claim=True)
        await session.commit()
    original = RunRepository.fail_stale_running

    async def claim_first(self: RunRepository, run_id: uuid.UUID, **kwargs: Any) -> Any:
        async with ctx.sessionmaker() as other:
            runs = RunRepository(other, ctx.tenant_a)
            prior = await runs.get(run_id)
            assert prior is not None
            assert (
                await runs.mark_running(
                    run_id, started_at=_NOW, expected_started_at=prior.started_at
                )
                is not None
            )
            await other.commit()
        return await original(self, run_id, **kwargs)

    monkeypatch.setattr(RunRepository, "fail_stale_running", claim_first)
    result = await _reap(_NOW)
    assert result.scanned == 1 and result.reaped == 0
    async with ctx.sessionmaker() as session:
        run = await RunRepository(session, ctx.tenant_a).get(run_id)
        assert run is not None and run.status is RunStatus.RUNNING
    assert await _finished_audit_count(ctx, run_id) == 0
    assert await _delivery_count(ctx, run_id) == 0


# --- Celery wrapper: an exception after the claim still terminalises --------


def test_celery_wrapper_fails_claimed_run_when_execute_run_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wrapper must not merely log: a claimed (running) run lands Failed.

    Drives the real sync task body with ``execute_run`` faked to raise after
    the claim. A second (redelivered) invocation is idempotent: no second
    ``run.finished`` audit and no second delivery.
    """
    from app.tasks import run_assistant as task_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'wrapper.db'}", poolclass=NullPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk(connection: object, _record: object) -> None:
        connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda settings=None: factory)

    async def seed() -> tuple[uuid.UUID, uuid.UUID]:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Acme")
            alice = await UserRepository(session, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(session, tenant.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            assistants = AssistantRepository(session, tenant.id)
            assistant = await assistants.create(
                owner_id=alice.id,
                name="Weekly",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=bob.id,
            )
            await assistants.update(assistant.id, fields={"status": AssistantStatus.PUBLISHED})
            published = await assistants.get(assistant.id)
            version = await AssistantVersionRepository(session, tenant.id).add(
                assistant_id=assistant.id,
                version=1,
                author_id=alice.id,
                config=config_from_assistant(published),
            )
            run = await RunRepository(session, tenant.id).create(
                owner_id=alice.id,
                assistant_id=assistant.id,
                assistant_version_id=version.id,
                trigger=RunTrigger.MANUAL,
            )
            # The claim already happened (the worker died/raised mid-run).
            await session.execute(
                update(models.Run)
                .where(models.Run.id == run.id)
                .values(status=RunStatus.RUNNING.value, started_at=datetime.now(UTC))
            )
            await session.commit()
            return tenant.id, run.id

    async def read_back(tenant_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
        async with factory() as session:
            run = await RunRepository(session, tenant_id).get(run_id)
            assert run is not None
            recent = await AuditEventRepository(session, tenant_id).list_recent(limit=200)
            deliveries = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(models.RunDelivery)
                        .where(models.RunDelivery.run_id == run_id)
                    )
                ).scalar_one()
            )
            return {
                "status": run.status,
                "finished_at": run.finished_at,
                "error_code": run.error.code if run.error else None,
                "finished_audits": sum(
                    1 for e in recent if e.resource_id == str(run_id) and e.action == "run.finished"
                ),
                "deliveries": deliveries,
            }

    tenant_id, run_id = asyncio.run(seed())

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("worker blew up after the claim")

    monkeypatch.setattr(task_module, "execute_run", _boom)
    monkeypatch.setattr(task_module, "run_task", lambda coro: asyncio.run(coro))

    try:
        result = task_module.run_assistant(str(run_id), str(tenant_id))
        assert result == {"run_id": str(run_id), "status": "failed"}
        after = asyncio.run(read_back(tenant_id, run_id))
        assert after["status"] is RunStatus.FAILED
        assert after["finished_at"] is not None
        assert after["error_code"] == "internal_error"
        assert after["finished_audits"] == 1
        assert after["deliveries"] == 1

        # Redelivery after the terminal: nothing is duplicated.
        again = task_module.run_assistant(str(run_id), str(tenant_id))
        assert again == {"run_id": str(run_id), "status": "failed"}
        after_again = asyncio.run(read_back(tenant_id, run_id))
        assert after_again["finished_audits"] == 1
        assert after_again["deliveries"] == 1
    finally:
        asyncio.run(engine.dispose())
