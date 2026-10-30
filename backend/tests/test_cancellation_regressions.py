"""Answer cancellation and hard spend bounds (#574, spec 0028).

The fakes use event handshakes. ``wait_for`` is only a deadlock guard: no test
asserts elapsed time or relies on a scheduler sleep to reach a cancellation point.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.principal import Principal
from app.db import models
from app.db.repositories import (
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.domain.llm import StreamEvent, TokenUsage, ToolCall
from app.domain.retrieval import RetrievedPassage
from app.domain.tools import ToolHandlerResult
from app.llm import LlmProviderError
from app.realtime.backplane import InMemoryBackplane, is_terminal
from app.services import chat_runtime
from app.services.answer_budgets import AnswerBudgets
from app.services.assistant_runtime import AssistantRunConfig
from app.services.chat_runtime import ChatRuntime
from app.services.tools.types import ToolContext, ToolDefinition
from tests._db_helpers import copy_sqlite_schema
from tests.test_chat_runtime import (
    _Ctx,
    _drain,
    _FakeRetrieval,
    _passage,
    _runtime,
    _ScriptedGateway,
)


@pytest.fixture(autouse=True)
def deterministic_token_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tokenizer behavior is independent of cancellation/spend limits; no provider
    # catalog or model-specific tokenizer is needed for these adapter fakes.
    monkeypatch.setattr(ChatRuntime, "_token_counter_for", lambda self, model: len)


@pytest_asyncio.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[_Ctx]:
    # Cancellation may invalidate a checked-out SQLite connection. A file keeps
    # schema/state available to the independent cancellation-finalisation session.
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'cancel.sqlite3').as_posix()}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(copy_sqlite_schema)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Cancellation tests")
            user = await UserRepository(seed, tenant.id).create(
                email="cancel@example.test", password_hash="x", roles=[Role.MEMBER]
            )
            collection = await CollectionRepository(seed, tenant.id).create(
                owner_id=user.id, name="Evidence"
            )
            document = await DocumentRepository(seed, tenant.id).create(
                owner_id=user.id,
                collection_id=collection.id,
                filename="source.txt",
                mime_type="text/plain",
                size_bytes=10,
                storage_key=f"{tenant.id}/source.txt",
                acl_enforced=False,
            )
            chunks = await ChunkRepository(seed, tenant.id).replace_for_document(
                document.id, [ChunkInput(text="Source evidence.", char_start=0, char_end=16)]
            )
            chat = await ChatSessionRepository(seed, tenant.id).create(
                owner_id=user.id, model="test/model", title="Cancellation"
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                principal=Principal(user_id=user.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
                session_id=chat.id,
                document_id=document.id,
                chunk_id=chunks[0].id,
                tenant_id=tenant.id,
            )
    finally:
        await engine.dispose()


class _ParkedGateway:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.calls = 0

    async def stream_tools(self, messages: object, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        try:
            yield StreamEvent(text="Partial answer. ")
            self.entered.set()
            await self.release.wait()
            yield StreamEvent(text="LATE OUTPUT")
            yield StreamEvent(finish_reason="stop")
        finally:
            self.closed.set()


async def _run(runtime: ChatRuntime, context: _Ctx, stream_id: str, **kwargs: Any) -> bool:
    return await runtime.run(
        stream_id=stream_id,
        session_id=context.session_id,
        question="Summarize the available evidence.",
        model="test/model",
        history=[],
        collection_ids=None,
        **kwargs,
    )


async def _assert_cancelled_database(context: _Ctx, stream_id: str, *, partial: str) -> None:
    async with context.sessionmaker() as session:
        messages = list((await session.scalars(select(models.Message))).all())
        assert len(messages) == 1
        assert messages[0].content == partial
        assert messages[0].finish_reason == "cancelled"
        assert messages[0].tenant_id == context.tenant_id
        for model in (models.Citation, models.WebCitation, models.Artifact):
            assert list((await session.scalars(select(model))).all()) == []
        audits = list(
            (
                await session.scalars(
                    select(models.AuditEvent).where(models.AuditEvent.action == "answer.cancelled")
                )
            ).all()
        )
        assert len(audits) == 1
        assert audits[0].tenant_id == context.tenant_id
        assert audits[0].actor_id == context.principal.user_id
        assert audits[0].event_metadata["stream_id"] == stream_id


async def test_remote_stop_interrupts_provider_and_keeps_only_cancelled_partial(ctx: _Ctx) -> None:
    """The remote signal interrupts I/O; releasing it later cannot save new output."""
    backplane = InMemoryBackplane()
    gateway = _ParkedGateway()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    task = asyncio.create_task(_run(runtime, ctx, stream_id))
    try:
        entered = asyncio.create_task(gateway.entered.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, entered}, timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done and entered not in done:
                early = await _drain(backplane, stream_id)
                pytest.fail(f"Runtime finished before provider handshake: {early}")
            assert entered in done, "The provider never reached its handshake."
        finally:
            entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)
        # These are the remote endpoint's operations: no local task reference.
        await backplane.signal_cancel(stream_id)
        await backplane.signal_cancel(stream_id)
        assert await asyncio.wait_for(asyncio.shield(task), timeout=30) is False
        assert gateway.closed.is_set()
        envelopes = await _drain(backplane, stream_id)
        assert [env["type"] for env in envelopes if is_terminal(env)] == ["cancelled"]
        assert envelopes[-1]["type"] == "cancelled"
        assert gateway.calls == 1
        assert "LATE OUTPUT" not in str(envelopes)
        await _assert_cancelled_database(ctx, stream_id, partial="Partial answer. ")
        # A delayed provider result has no surviving consumer after acknowledgement.
        gateway.release.set()
        await _assert_cancelled_database(ctx, stream_id, partial="Partial answer. ")
    finally:
        gateway.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _CancellationSuppressingRetrieval(_FakeRetrieval):
    """An uncooperative adapter returns a result after receiving cancellation."""

    def __init__(self, passages: list[RetrievedPassage]) -> None:
        super().__init__(passages)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def search_text(self, **kwargs: Any) -> list[RetrievedPassage]:
        self.queries.append(str(kwargs["query"]))
        if len(self.queries) == 1:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        return list(self._passages)


async def test_remote_stop_discards_late_tool_result_and_prevents_serial_tail(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.tools import runner as tool_runner

    original_tool = tool_runner.get_tool

    def without_deadline(name: str) -> ToolDefinition:
        definition = original_tool(name)
        return replace(definition, timeout_seconds=None) if name == "search_text" else definition

    # Only the user Stop signal may interrupt this fake; the native tool's own
    # timeout must not accidentally satisfy the cancellation handshake.
    monkeypatch.setattr(tool_runner, "get_tool", without_deadline)
    retrieval = _CancellationSuppressingRetrieval(
        [_passage(ctx.document_id, ctx.chunk_id, "source.txt")]
    )
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id="first", name="search_text", arguments={"query": "first"}),
                        ToolCall(id="second", name="search_text", arguments={"query": "second"}),
                    ),
                    finish_reason="tool_calls",
                )
            ]
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx, gateway=gateway, retrieval=retrieval, backplane=backplane, tool_concurrency=1
    )
    task = asyncio.create_task(_run(runtime, ctx, stream_id))
    try:
        await asyncio.wait_for(retrieval.entered.wait(), timeout=30)
        await backplane.signal_cancel(stream_id)
        assert await asyncio.wait_for(asyncio.shield(task), timeout=30) is False
        assert retrieval.cancelled.is_set()
        assert retrieval.queries == ["first"]
        assert gateway.calls == 1
        envelopes = await _drain(backplane, stream_id)
        assert [env["type"] for env in envelopes if is_terminal(env)] == ["cancelled"]
        assert not any(env.get("name") == "tool_result" for env in envelopes)
        await _assert_cancelled_database(ctx, stream_id, partial="")
        async with ctx.sessionmaker() as session:
            assert list((await session.scalars(select(models.ToolInvocation))).all()) == []
    finally:
        retrieval.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stop_already_signalled_before_start_makes_no_model_call(ctx: _Ctx) -> None:
    gateway = _ScriptedGateway([[StreamEvent(text="Must not run.")]])
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    await backplane.signal_cancel(stream_id)
    runtime = _runtime(ctx, gateway=gateway, retrieval=_FakeRetrieval([]), backplane=backplane)
    assert await _run(runtime, ctx, stream_id) is False
    assert gateway.calls == 0
    envelopes = await _drain(backplane, stream_id)
    assert [env["type"] for env in envelopes if is_terminal(env)] == ["cancelled"]
    await _assert_cancelled_database(ctx, stream_id, partial="")


class _RetryingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_tools(self, messages: object, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        raise LlmProviderError("The provider is unavailable.", retryable=True)
        yield  # pragma: no cover — establish the async-generator protocol


async def test_cancel_between_retry_attempts_prevents_another_gateway_call(ctx: _Ctx) -> None:
    gateway = _RetryingGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    async def cancel_during_backoff(_delay: float) -> None:
        runtime.request_cancel()

    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        retry_sleep=cancel_during_backoff,
    )
    assert await _run(runtime, ctx, stream_id) is False
    assert gateway.calls == 1
    envelopes = await _drain(backplane, stream_id)
    assert [env["type"] for env in envelopes if is_terminal(env)] == ["cancelled"]
    await _assert_cancelled_database(ctx, stream_id, partial="")


async def test_model_call_budget_counts_retry_attempts(ctx: _Ctx) -> None:
    gateway = _RetryingGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    async def no_backoff(_delay: float) -> None:
        return None

    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        retry_sleep=no_backoff,
        budgets=AnswerBudgets(max_model_calls=2, interactive_max_attempts=3),
    )
    assert await _run(runtime, ctx, stream_id) is False
    assert gateway.calls == 2
    envelopes = await _drain(backplane, stream_id)
    assert [env["type"] for env in envelopes if is_terminal(env)] == ["error"]
    assert "budget" in str(envelopes[-1]["problem"]).lower()


async def test_external_budget_caps_looping_mcp_calls(ctx: _Ctx) -> None:
    calls: list[dict[str, Any]] = []

    async def lookup(args: dict[str, Any], _context: ToolContext) -> ToolHandlerResult:
        calls.append(args)
        return ToolHandlerResult(content="Retrieved external result.", summary="retrieved")

    definition = ToolDefinition(
        name="mcp:test:lookup",
        description="Look up a public reference.",
        json_schema={"type": "object", "properties": {"index": {"type": "integer"}}},
        handler=lookup,
    )

    async def mcp_factory(_session: AsyncSession) -> dict[str, ToolDefinition]:
        return {definition.name: definition}

    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(id=f"lookup-{i}", name=definition.name, arguments={"index": i}),
                    ),
                    finish_reason="tool_calls",
                )
            ]
            for i in range(100)
        ],
        synthesis=[StreamEvent(text="Finished."), StreamEvent(finish_reason="stop")],
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        mcp_tools_factory=mcp_factory,
        budgets=AnswerBudgets(max_tool_turns=100, max_external_calls=2),
    )
    assert (
        await _run(
            runtime,
            ctx,
            stream_id,
            assistant_config=AssistantRunConfig(
                system_prompt="Answer from available evidence.",
                allowed=frozenset({definition.name}),
                collection_ids=None,
                model=None,
            ),
        )
        is True
    )
    envelopes = await _drain(backplane, stream_id)
    assert len(calls) == 2, envelopes
    assert gateway.auto_calls == 3
    assert gateway.synthesis_calls == 1
    envelopes = await _drain(backplane, stream_id)
    assert [env["type"] for env in envelopes if is_terminal(env)] == ["done"]


async def test_wall_clock_budget_covers_whole_answer_without_forced_synthesis(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired absolute deadline must stop preparation, not buy another turn."""
    monkeypatch.setattr(
        ChatRuntime,
        "_answer_deadline_at",
        lambda _self, _seconds: asyncio.get_running_loop().time() - 1,
        raising=False,
    )
    gateway = _ScriptedGateway([[StreamEvent(text="Must not run.")]])
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        budgets=AnswerBudgets(wall_clock_seconds=600),
    )
    assert await _run(runtime, ctx, stream_id) is False
    assert gateway.calls == 0
    envelopes = await _drain(backplane, stream_id)
    assert [env["type"] for env in envelopes if is_terminal(env)] == ["error"]
    assert "budget" in str(envelopes[-1]["problem"]).lower()
    async with ctx.sessionmaker() as session:
        assert list((await session.scalars(select(models.Message))).all()) == []


async def test_expired_answer_deadline_discards_provider_event_after_suppressed_cancel(
    ctx: _Ctx,
) -> None:
    """A provider cannot defeat the absolute deadline by absorbing task cancellation."""

    class _LateProvider:
        calls = 0
        suppressed_cancel = False
        closed = False

        async def stream_tools(self, messages: object, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            try:
                yield StreamEvent(text="Before the deadline. ")
                # Expire the runtime's absolute budget deterministically. The
                # separate timeout context need not wait for real clock time.
                runtime._answer_deadline = asyncio.get_running_loop().time() - 1
                pending_event = asyncio.current_task()
                assert pending_event is not None
                pending_event.cancel()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.suppressed_cancel = True
                yield StreamEvent(text="LATE EVENT MUST NOT BE CONSUMED")
                yield StreamEvent(finish_reason="stop")
            finally:
                self.closed = True

    gateway = _LateProvider()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        budgets=AnswerBudgets(wall_clock_seconds=600, max_model_calls=5),
    )
    assert await _run(runtime, ctx, stream_id) is False
    assert gateway.suppressed_cancel is True
    assert gateway.closed is True
    assert gateway.calls == 1
    envelopes = await _drain(backplane, stream_id)
    assert [env["type"] for env in envelopes if is_terminal(env)] == ["error"]
    assert envelopes[-1]["problem"]["code"] == "answer_budget_exhausted"
    assert "LATE EVENT" not in str(envelopes)
    async with ctx.sessionmaker() as session:
        for model in (models.Message, models.Citation, models.WebCitation, models.Artifact):
            assert list((await session.scalars(select(model))).all()) == []


async def test_stop_racing_final_commit_keeps_completed_answer_and_usage(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final commit wins atomically, with one done and honest completed usage."""
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    remote_observed = asyncio.Event()
    commits = 0

    def session_factory() -> AsyncSession:
        session = ctx.sessionmaker()
        original_commit = session.commit

        async def commit() -> None:
            nonlocal commits
            commits += 1
            # The first commit reserves handles; the second commits the answer.
            if commits == 2:
                commit_entered.set()
                await release_commit.wait()
            await original_commit()

        session.commit = commit  # type: ignore[method-assign]
        return session

    class _ObservedBackplane(InMemoryBackplane):
        async def wait_cancel(self, stream_id: str) -> None:
            await super().wait_cancel(stream_id)
            remote_observed.set()

    outcome_records: list[dict[str, Any]] = []

    def record(event: str, **fields: Any) -> None:
        if event == "llm.answer_outcome":
            outcome_records.append(fields)

    monkeypatch.setattr(chat_runtime.log, "info", record)
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(text="The completed answer."),
                StreamEvent(
                    finish_reason="stop",
                    usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
                ),
            ]
        ]
    )
    backplane = _ObservedBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        sessionmaker=session_factory,
    )
    task = asyncio.create_task(_run(runtime, ctx, stream_id))
    try:
        await asyncio.wait_for(commit_entered.wait(), timeout=30)
        assert runtime.request_cancel() is False
        await backplane.signal_cancel(stream_id)
        await asyncio.wait_for(remote_observed.wait(), timeout=30)
        assert not task.done()
        release_commit.set()
        assert await asyncio.wait_for(asyncio.shield(task), timeout=30) is True
        envelopes = await _drain(backplane, stream_id)
        assert [env["type"] for env in envelopes if is_terminal(env)] == ["done"]
        assert gateway.calls == 1
        async with ctx.sessionmaker() as session:
            messages = list((await session.scalars(select(models.Message))).all())
            assert len(messages) == 1
            assert messages[0].content == "The completed answer."
            assert messages[0].finish_reason is None
            cancellations = list(
                (
                    await session.scalars(
                        select(models.AuditEvent).where(
                            models.AuditEvent.action == "answer.cancelled"
                        )
                    )
                ).all()
            )
            assert cancellations == []
        assert len(outcome_records) == 1
        assert outcome_records[0]["outcome"] == "complete"
        assert outcome_records[0]["model_calls"] == 1
        assert outcome_records[0]["prompt_tokens"] == 11
        assert outcome_records[0]["completion_tokens"] == 7
    finally:
        release_commit.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_shutdown_drains_user_cancellation_finalizer_before_producer_exits(ctx: _Ctx) -> None:
    """A second task cancel must not orphan the child saving the stopped partial."""
    finalizer_entered = asyncio.Event()
    release_finalizer = asyncio.Event()
    finalizer_committed = asyncio.Event()
    remote_observed = asyncio.Event()
    finalizer_tasks: list[asyncio.Task[Any]] = []

    def session_factory() -> AsyncSession:
        session = ctx.sessionmaker()
        original_commit = session.commit

        async def commit() -> None:
            if runtime._finalising_cancel:
                child = asyncio.current_task()
                assert child is not None
                finalizer_tasks.append(child)
                finalizer_entered.set()
                await release_finalizer.wait()
                await original_commit()
                finalizer_committed.set()
            else:
                await original_commit()

        session.commit = commit  # type: ignore[method-assign]
        return session

    class _ObservedBackplane(InMemoryBackplane):
        async def wait_cancel(self, stream_id: str) -> None:
            await super().wait_cancel(stream_id)
            remote_observed.set()

    gateway = _ParkedGateway()
    backplane = _ObservedBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        sessionmaker=session_factory,
    )
    task = asyncio.create_task(_run(runtime, ctx, stream_id))
    try:
        await asyncio.wait_for(gateway.entered.wait(), timeout=30)
        assert runtime.request_cancel() is True
        await asyncio.wait_for(finalizer_entered.wait(), timeout=30)
        # Finish the control watcher before probing producer shutdown. Its own
        # cleanup must not be mistaken for the producer draining its finalizer.
        await backplane.signal_cancel(stream_id)
        await asyncio.wait_for(remote_observed.wait(), timeout=30)
        task.cancel()
        checkpoint = asyncio.Event()
        # Task.cancel schedules the producer's wakeup first; call_soon callbacks
        # run FIFO, so this marker runs after it processes the second cancellation.
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        assert runtime._running_task is task
        assert not task.done()
        assert not finalizer_committed.is_set()
        release_finalizer.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        assert finalizer_committed.is_set()
        assert finalizer_tasks and all(child.done() for child in finalizer_tasks)
        envelopes = await _drain(backplane, stream_id)
        assert [env["type"] for env in envelopes if is_terminal(env)] == ["cancelled"]
        await _assert_cancelled_database(ctx, stream_id, partial="Partial answer. ")
    finally:
        gateway.release.set()
        release_finalizer.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *finalizer_tasks, return_exceptions=True)


@pytest.mark.parametrize("shutdown", [False, True])
async def test_stop_during_provider_error_settlement_preserves_error_terminal(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch, shutdown: bool
) -> None:
    """Once failure settlement starts, Stop cannot interrupt its terminal publish."""
    salvage_entered = asyncio.Event()
    release_salvage = asyncio.Event()
    gateway = _RetryingGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        budgets=AnswerBudgets(interactive_max_attempts=1),
    )

    async def salvage(_session_id: uuid.UUID, _message_id: uuid.UUID) -> None:
        salvage_entered.set()
        await release_salvage.wait()

    monkeypatch.setattr(runtime, "_salvage_usage_after_failure", salvage)
    task = asyncio.create_task(_run(runtime, ctx, stream_id))
    try:
        await asyncio.wait_for(salvage_entered.wait(), timeout=30)
        assert runtime.request_cancel() is False
        assert not task.done()
        if shutdown:
            task.cancel()
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            assert runtime._running_task is task
            assert not task.done()
        release_salvage.set()
        if shutdown:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=30)
        else:
            assert await asyncio.wait_for(asyncio.shield(task), timeout=30) is False
        envelopes = await _drain(backplane, stream_id)
        assert [env["type"] for env in envelopes if is_terminal(env)] == ["error"]
        assert gateway.calls == 1
        async with ctx.sessionmaker() as session:
            cancellations = list(
                (
                    await session.scalars(
                        select(models.AuditEvent).where(
                            models.AuditEvent.action == "answer.cancelled"
                        )
                    )
                ).all()
            )
            assert cancellations == []
    finally:
        release_salvage.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stop_racing_ask_user_commit_preserves_question_and_completed_usage(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The question-terminal branch obeys the same commit and usage guarantees."""
    commit_entered = asyncio.Event()
    release_commit = asyncio.Event()
    commits = 0

    def session_factory() -> AsyncSession:
        session = ctx.sessionmaker()
        original_commit = session.commit

        async def commit() -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                commit_entered.set()
                await release_commit.wait()
            await original_commit()

        session.commit = commit  # type: ignore[method-assign]
        return session

    outcomes: list[dict[str, Any]] = []

    def record(event: str, **fields: Any) -> None:
        if event == "llm.answer_outcome":
            outcomes.append(fields)

    monkeypatch.setattr(chat_runtime.log, "info", record)
    gateway = _ScriptedGateway(
        [
            [
                StreamEvent(
                    tool_calls=(
                        ToolCall(
                            id="question-1",
                            name="ask_user",
                            arguments={
                                "question": "Which source should I use?",
                                "options": [{"label": "First"}, {"label": "Second"}],
                                "allow_free_text": True,
                            },
                        ),
                    ),
                    finish_reason="tool_calls",
                    usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
                )
            ]
        ]
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        sessionmaker=session_factory,
    )
    task = asyncio.create_task(_run(runtime, ctx, stream_id))
    try:
        await asyncio.wait_for(commit_entered.wait(), timeout=30)
        assert runtime.request_cancel() is False
        assert not task.done()
        release_commit.set()
        assert await asyncio.wait_for(asyncio.shield(task), timeout=30) is True
        envelopes = await _drain(backplane, stream_id)
        assert [env["type"] for env in envelopes if is_terminal(env)] == ["done"]
        assert envelopes[-1]["data"]["finishReason"] == "ask_user"
        assert gateway.calls == 1
        async with ctx.sessionmaker() as session:
            messages = list((await session.scalars(select(models.Message))).all())
            assert len(messages) == 1
            assert messages[0].content == "Which source should I use?"
            assert messages[0].question is not None
            assert messages[0].finish_reason is None
            cancellations = list(
                (
                    await session.scalars(
                        select(models.AuditEvent).where(
                            models.AuditEvent.action == "answer.cancelled"
                        )
                    )
                ).all()
            )
            assert cancellations == []
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "complete"
        assert outcomes[0]["prompt_tokens"] == 11
        assert outcomes[0]["completion_tokens"] == 7
    finally:
        release_commit.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_accepted_stop_survives_provider_translating_cancel_to_terminal_error(
    ctx: _Ctx,
) -> None:
    """An adapter's cancellation-to-error translation cannot erase accepted Stop."""

    class _CancellationTranslatingProvider:
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0
            self.translated_cancel = False
            self.closed = False

        async def stream_tools(self, messages: object, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            try:
                yield StreamEvent(text="Partial answer. ")
                self.entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.translated_cancel = True
                    raise LlmProviderError(
                        "The provider stream stopped.", retryable=False
                    ) from None
                yield StreamEvent(text="Late output must not appear.")
                yield StreamEvent(finish_reason="stop")
            finally:
                self.closed = True

    gateway = _CancellationTranslatingProvider()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        text_coalesce_chars=1,
    )
    task = asyncio.create_task(_run(runtime, ctx, stream_id))
    try:
        await asyncio.wait_for(gateway.entered.wait(), timeout=30)
        await backplane.signal_cancel(stream_id)
        assert await asyncio.wait_for(asyncio.shield(task), timeout=30) is False
        assert gateway.translated_cancel is True
        assert gateway.closed is True
        assert gateway.calls == 1
        envelopes = await _drain(backplane, stream_id)
        assert [env["type"] for env in envelopes if is_terminal(env)] == ["cancelled"]
        assert "Late output" not in str(envelopes)
        await _assert_cancelled_database(ctx, stream_id, partial="Partial answer. ")
    finally:
        gateway.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
