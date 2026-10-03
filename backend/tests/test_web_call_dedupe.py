"""Per-answer deduplication contract for the public static web_search tool (#598)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db import models
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    TenantRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import Role
from app.domain.llm import ToolCall
from app.domain.tools import RiskTier, ToolHandlerResult
from app.services.audit import AuditSink
from app.services.tools import runner as runner_module
from app.services.tools.runner import ToolRunner
from app.services.tools.types import ToolContext, ToolDefinition

import app.db.models  # noqa: F401  isort: skip


class _World:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.principal = Principal(user_id=user_id, tenant_id=tenant_id, roles=(Role.MEMBER,))
        self.context = ToolContext(principal=self.principal, retrieval=object())  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Web dedupe test")
            user = await UserRepository(session, tenant.id).create(
                email=f"dedupe-{uuid.uuid4()}@example.test",
                password_hash="x",
                roles=[Role.MEMBER],
            )
            await session.commit()
            yield _World(session, tenant.id, user.id)
    finally:
        await engine.dispose()


def _definition(handler: Any, *, timeout_seconds: float | None = 2.0) -> ToolDefinition:
    """A static registry web_search definition with its governance kept intact."""
    return ToolDefinition(
        name="web_search",
        description="Public web search",
        json_schema={"type": "object"},
        handler=handler,
        risk_tier=RiskTier.T0,
        read_only=True,
        requires_approval=False,
        timeout_seconds=timeout_seconds,
        default_offered=False,
    )


def _patch_web_search(monkeypatch: pytest.MonkeyPatch, definition: ToolDefinition) -> None:
    def get_tool(name: str) -> ToolDefinition:
        if name == "web_search":
            return definition
        from app.services.tools.registry import UnknownToolError

        raise UnknownToolError(name)

    monkeypatch.setattr(runner_module, "get_tool", get_tool)


def _runner(
    world: _World,
    *,
    allowed: frozenset[str] = frozenset({"web_search"}),
    extra_tools: Mapping[str, ToolDefinition] | None = None,
) -> ToolRunner:
    return ToolRunner(
        allowed=allowed,
        invocations=ToolInvocationRepository(world.session, world.tenant_id),
        audit=AuditSink(AuditEventRepository(world.session, world.tenant_id)),
        actor=AuditActor.user(world.user_id),
        request_id="web-dedupe-test",
        source_ip="127.0.0.1",
        session_id=None,
        extra_tools=extra_tools,
    )


def _call(call_id: str, arguments: dict[str, object] | None = None) -> ToolCall:
    return ToolCall(
        id=call_id, name="web_search", arguments=arguments or {"query": "quarterly results", "k": 3}
    )


async def _counts(world: _World) -> tuple[int, int, list[str]]:
    audits = list((await world.session.execute(select(models.AuditEvent))).scalars().all())
    invocations = list((await world.session.execute(select(models.ToolInvocation))).scalars().all())
    return len(invocations), len(audits), [event.action for event in audits]


async def _audit_rows(world: _World) -> list[models.AuditEvent]:
    return list((await world.session.execute(select(models.AuditEvent))).scalars().all())


def _web_handler(
    counter: list[int],
    *,
    entered: asyncio.Event | None = None,
    release: asyncio.Event | None = None,
):
    async def handler(args: dict[str, Any], context: ToolContext) -> ToolHandlerResult:
        counter[0] += 1
        if entered is not None:
            entered.set()
        if release is not None:
            await release.wait()
        return ToolHandlerResult(
            content=f"results for {args['query']}",
            summary="1 public result",
            payload={"query": args["query"], "k": args.get("k"), "execution": counter[0]},
        )

    return handler


async def test_identical_web_searches_coalesce_sequentially_but_keep_per_call_trace(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same canonical args execute once; each caller retains its own result and audit IDs."""
    executions = [0]
    _patch_web_search(monkeypatch, _definition(_web_handler(executions)))
    runner = _runner(world)

    first = await runner.run(
        call=_call("web-call-a", {"query": "  quarterly results  ", "k": 3}),
        context=world.context,
    )
    second = await runner.run(
        call=_call("web-call-b", {"k": 3, "query": "quarterly results"}),
        context=world.context,
    )

    assert executions == [1]
    assert first.ok and second.ok
    assert first.call_id == "web-call-a" and second.call_id == "web-call-b"
    assert first.call_id != second.call_id
    assert first.payload == second.payload
    invocation_count, audit_count, actions = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4
    assert actions.count(AuditAction.TOOL_INVOKED.value) == 2
    assert actions.count(AuditAction.TOOL_RESULT.value) == 2
    provenance: dict[str, list[dict[str, object]]] = {}
    for event in await _audit_rows(world):
        if event.action in {AuditAction.TOOL_INVOKED.value, AuditAction.TOOL_RESULT.value}:
            metadata = dict(event.event_metadata)
            provenance.setdefault(str(metadata["call_id"]), []).append(metadata)
    assert set(provenance) == {"web-call-a", "web-call-b"}
    assert all(len(records) == 2 for records in provenance.values())
    assert all(
        record["execution_call_id"] == "web-call-a"
        for records in provenance.values()
        for record in records
    )
    assert all(record["deduplicated"] is False for record in provenance["web-call-a"])
    assert all(record["deduplicated"] is True for record in provenance["web-call-b"])


async def test_identical_concurrent_web_searches_share_one_execution_and_keep_two_results(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    executions = [0]
    entered = asyncio.Event()
    release = asyncio.Event()
    _patch_web_search(
        monkeypatch, _definition(_web_handler(executions, entered=entered, release=release))
    )
    runner = _runner(world)

    first_task = asyncio.create_task(runner.run(call=_call("parallel-a"), context=world.context))
    await entered.wait()
    second_task = asyncio.create_task(runner.run(call=_call("parallel-b"), context=world.context))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert executions == [1]
    assert first.ok and second.ok
    assert {first.call_id, second.call_id} == {"parallel-a", "parallel-b"}
    assert first.payload == second.payload
    invocation_count, audit_count, actions = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4
    assert actions.count(AuditAction.TOOL_INVOKED.value) == 2
    assert actions.count(AuditAction.TOOL_RESULT.value) == 2


async def test_same_query_with_different_k_is_a_distinct_web_search(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    executions = [0]
    _patch_web_search(monkeypatch, _definition(_web_handler(executions)))
    runner = _runner(world)

    await runner.run(call=_call("k-3", {"query": "same query", "k": 3}), context=world.context)
    await runner.run(call=_call("k-5", {"query": "same query", "k": 5}), context=world.context)

    assert executions == [2]
    invocation_count, audit_count, _ = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4


async def test_completed_empty_web_result_is_reused_as_a_success(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    executions = [0]

    async def empty_success(args: dict[str, Any], context: ToolContext) -> ToolHandlerResult:
        executions[0] += 1
        return ToolHandlerResult(content="", summary="No public results", payload={"results": []})

    _patch_web_search(monkeypatch, _definition(empty_success))
    runner = _runner(world)
    first = await runner.run(call=_call("empty-a"), context=world.context)
    second = await runner.run(call=_call("empty-b"), context=world.context)

    assert executions == [1]
    assert first.ok and second.ok
    assert first.content == second.content == ""
    assert first.payload == second.payload == {"results": []}
    assert first.call_id == "empty-a" and second.call_id == "empty-b"
    invocation_count, audit_count, _ = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4


async def test_dynamic_tool_named_web_search_is_never_coalesced(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-run dynamic tools stay outside static public-web coalescing."""
    executions = [0]
    definition = _definition(_web_handler(executions))
    # The dynamic closure wins registry resolution and must not join static cache.
    _patch_web_search(monkeypatch, _definition(_web_handler([0])))
    runner = _runner(world, extra_tools={"web_search": definition})

    first = await runner.run(call=_call("dynamic-a"), context=world.context)
    second = await runner.run(call=_call("dynamic-b"), context=world.context)

    assert executions == [2]
    assert first.ok and second.ok
    assert first.call_id == "dynamic-a" and second.call_id == "dynamic-b"
    invocation_count, audit_count, _ = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4


async def test_other_static_read_only_tools_are_not_coalesced(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The optimization is scoped to public web_search, not a generic tool cache."""
    executions = [0]
    definition = ToolDefinition(
        name="search_text",
        description="Corpus search",
        json_schema={"type": "object"},
        handler=_web_handler(executions),
        risk_tier=RiskTier.T0,
        read_only=True,
        requires_approval=False,
    )

    def get_tool(name: str) -> ToolDefinition:
        if name == "search_text":
            return definition
        from app.services.tools.registry import UnknownToolError

        raise UnknownToolError(name)

    monkeypatch.setattr(runner_module, "get_tool", get_tool)
    runner = _runner(world, allowed=frozenset({"search_text"}))
    args = {"query": "same corpus query", "k": 3}
    for call_id in ("corpus-a", "corpus-b"):
        await runner.run(
            call=ToolCall(id=call_id, name="search_text", arguments=args),
            context=world.context,
        )

    assert executions == [2]
    invocation_count, audit_count, _ = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4


async def test_denied_repeated_web_searches_never_reach_handler_or_coalescing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    executions = [0]
    _patch_web_search(monkeypatch, _definition(_web_handler(executions)))
    runner = _runner(world, allowed=frozenset())

    first = await runner.run(call=_call("denied-a"), context=world.context)
    second = await runner.run(call=_call("denied-b"), context=world.context)

    assert executions == [0]
    assert not first.ok and not second.ok
    assert first.call_id == "denied-a" and second.call_id == "denied-b"
    invocation_count, audit_count, actions = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4
    assert actions.count(AuditAction.TOOL_INVOKED.value) == 2
    assert actions.count(AuditAction.TOOL_RESULT.value) == 2


async def test_web_search_results_are_not_shared_between_answer_runners(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    executions = [0]
    _patch_web_search(monkeypatch, _definition(_web_handler(executions)))

    for call_id in ("answer-one", "answer-two"):
        result = await _runner(world).run(call=_call(call_id), context=world.context)
        assert result.ok and result.call_id == call_id

    assert executions == [2]
    invocation_count, audit_count, _ = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4


async def test_failed_web_search_is_not_cached_and_next_call_retries(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    executions = [0]

    async def sometimes_fails(args: dict[str, Any], context: ToolContext) -> ToolHandlerResult:
        executions[0] += 1
        if executions[0] == 1:
            raise RuntimeError("transient provider failure")
        return ToolHandlerResult(content="recovered", summary="recovered", payload={"retry": 2})

    _patch_web_search(monkeypatch, _definition(sometimes_fails))
    runner = _runner(world)

    failed = await runner.run(call=_call("attempt-one"), context=world.context)
    succeeded = await runner.run(call=_call("attempt-two"), context=world.context)

    assert executions == [2]
    assert not failed.ok
    assert succeeded.ok and succeeded.content == "recovered"
    assert failed.call_id == "attempt-one" and succeeded.call_id == "attempt-two"
    invocation_count, audit_count, _ = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4


async def test_cancelling_web_search_releases_producer_and_leaves_no_background_tasks(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    executions = [0]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cancellable(args: dict[str, Any], context: ToolContext) -> ToolHandlerResult:
        executions[0] += 1
        entered.set()
        await release.wait()
        return ToolHandlerResult(content="done", payload={"ok": True})

    _patch_web_search(monkeypatch, _definition(cancellable, timeout_seconds=None))
    runner = _runner(world)
    before = asyncio.all_tasks()
    producer = asyncio.create_task(runner.run(call=_call("cancel-me"), context=world.context))
    await entered.wait()
    producer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await producer

    pending_created = {
        task
        for task in asyncio.all_tasks()
        if task not in before and task is not asyncio.current_task() and not task.done()
    }
    assert pending_created == set(), f"dedupe left background tasks alive: {pending_created!r}"

    # Retrying on the same per-answer runner must not find the cancelled producer
    # entry; a new provider execution can finish normally.
    release.set()
    retry = await asyncio.wait_for(
        runner.run(call=_call("retry-after-cancel"), context=world.context), timeout=1.0
    )
    assert retry.ok and retry.call_id == "retry-after-cancel"
    assert executions == [2]


async def test_cancelled_web_search_consumer_does_not_cancel_shared_waiter(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One cancelled call gets its own cancellation trace; a peer can use the result."""
    executions = [0]
    entered = asyncio.Event()
    release = asyncio.Event()
    _patch_web_search(
        monkeypatch, _definition(_web_handler(executions, entered=entered, release=release))
    )
    runner = _runner(world)

    first_task = asyncio.create_task(
        runner.run(call=_call("cancelled-consumer"), context=world.context)
    )
    await entered.wait()
    second_task = asyncio.create_task(
        runner.run(call=_call("surviving-consumer"), context=world.context)
    )
    await asyncio.sleep(0)
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    release.set()
    surviving = await asyncio.wait_for(second_task, timeout=1.0)
    assert surviving.ok and surviving.call_id == "surviving-consumer"
    assert executions == [1]
    invocation_count, audit_count, actions = await _counts(world)
    assert invocation_count == 2
    assert audit_count == 4
    assert actions.count(AuditAction.TOOL_INVOKED.value) == 2
    assert actions.count(AuditAction.TOOL_RESULT.value) == 2
