"""Regression coverage for context-rejected audit snapshot restoration (#645)."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models
from app.db.repositories import AuditEventRepository, MessageRepository, ToolInvocationRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditEvent, AuditOutcome, ToolInvocation
from app.domain.retrieval import RetrievedPassage
from app.llm.context import ContextConfig
from app.realtime.backplane import InMemoryBackplane
from app.services.audit import AuditSink
from app.services.tools.context_audit import ContextAuditSink
from tests import test_chat_api as chat_api_tests
from tests import test_chat_runtime as chat_runtime_tests

# Reuse established SQLite fixtures without loading whole test modules as plugins.
seeded = chat_api_tests.seeded
sessionmaker = chat_api_tests.sessionmaker
ctx = chat_runtime_tests.ctx


async def _foreign_audit_event(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: UUID, actor_id: UUID
) -> AuditEvent:
    async with sessionmaker() as session:
        event = await AuditEventRepository(session, tenant_id).record(
            action=AuditAction.RETRIEVAL_QUERY.value,
            resource_type="search",
            outcome=AuditOutcome.ALLOWED,
            actor_id=actor_id,
            resource_id="query-hash",
            request_id="foreign-audit-snapshot",
            source_ip="192.0.2.10",
            metadata={"result_count": 1},
        )
        assert event.id is not None and event.ts != datetime.min
        await session.rollback()
        return event


async def _foreign_tool_invocation(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: UUID
) -> ToolInvocation:
    async with sessionmaker() as session:
        record = await ToolInvocationRepository(session, tenant_id).record(
            tool_name="search_text",
            args_hash="safe-hash",
            ok=True,
            duration_ms=17,
            result_summary="1 passage",
            ordinal=2,
        )
        assert record.id is not None and record.created_at != datetime.min
        await session.rollback()
        return record


async def test_audit_snapshot_rejects_foreign_tenant_before_insert(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: chat_api_tests._Seeded
) -> None:
    event = await _foreign_audit_event(sessionmaker, seeded.tenant_b, seeded.carol_id)

    async with sessionmaker() as session:
        repository = AuditEventRepository(session, seeded.tenant_a)
        with pytest.raises(ValueError, match="tenant does not match"):
            await repository.restore_context_rejected([event])
        stored = await session.scalar(
            select(models.AuditEvent.id).where(models.AuditEvent.id == event.id)
        )
    assert stored is None


async def test_audit_sink_rejects_foreign_tenant_snapshot_before_insert(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: chat_api_tests._Seeded
) -> None:
    event = await _foreign_audit_event(sessionmaker, seeded.tenant_b, seeded.carol_id)

    async with sessionmaker() as session:
        repository = AuditEventRepository(session, seeded.tenant_a)
        with pytest.raises(ValueError, match="tenant does not match"):
            await AuditSink(repository).restore_context_rejected([event])
        stored = await session.scalar(
            select(models.AuditEvent.id).where(models.AuditEvent.id == event.id)
        )
    assert stored is None


async def test_tool_invocation_snapshot_rejects_foreign_tenant_before_insert(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: chat_api_tests._Seeded
) -> None:
    record = await _foreign_tool_invocation(sessionmaker, seeded.tenant_b)
    record_id = record.id

    async with sessionmaker() as session:
        repository = ToolInvocationRepository(session, seeded.tenant_a)
        with pytest.raises(ValueError, match="tenant does not match"):
            await repository.restore_context_rejected([record])
        stored = await session.scalar(
            select(models.ToolInvocation.id).where(models.ToolInvocation.id == record_id)
        )
    assert stored is None


def _fake_event(tenant_id: UUID, values: dict[str, object]) -> AuditEvent:
    metadata = cast(dict[str, object], values["metadata"])
    outcome = cast(AuditOutcome, values["outcome"])
    actor_id = cast(UUID | None, values["actor_id"])
    raw_action = values["action"]
    action = raw_action.value if isinstance(raw_action, AuditAction) else str(raw_action)
    return AuditEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource_type=cast(str, values["resource_type"]),
        resource_id=cast(str, values["resource_id"]),
        outcome=outcome,
        request_id=cast(str, values["request_id"]),
        source_origin="client",
        source_ip=cast(str, values["source_ip"]),
        metadata=metadata,
        ts=datetime.now(UTC),
    )


class _FakeAuditRepository:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.durable_calls = 0

    async def record(self, **values: object) -> AuditEvent:
        return _fake_event(self.tenant_id, values)

    async def record_committed(self, **values: object) -> AuditEvent:
        self.durable_calls += 1
        return _fake_event(self.tenant_id, values)


async def test_context_audit_sink_keeps_only_deep_copied_nondurable_records() -> None:
    tenant_id = uuid4()
    actor = AuditActor.user(uuid4())
    repository = _FakeAuditRepository(tenant_id)
    sink = ContextAuditSink(repository)  # type: ignore[arg-type]
    metadata: dict[str, object] = {"nested": {"items": ["before"]}}

    transient = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=actor,
        resource_type="search",
        outcome=AuditOutcome.ALLOWED,
        resource_id="query-hash",
        request_id="request-1",
        source_ip="192.0.2.11",
        metadata=metadata,
    )
    cast(list[str], cast(dict[str, object], metadata["nested"])["items"]).append("after")
    assert cast(dict[str, object], sink.events[0].metadata["nested"])["items"] == ["before"]

    durable = await sink.emit(
        action=AuditAction.ANSWER_GENERATED,
        actor=actor,
        resource_type="answer",
        outcome=AuditOutcome.ALLOWED,
        resource_id="message-1",
        request_id="request-2",
        source_ip="192.0.2.11",
        metadata={"citation_count": 1},
        durable=True,
    )
    assert repository.durable_calls == 1
    assert [event.id for event in sink.events] == [transient.id]
    assert durable.id != transient.id


async def test_context_restore_failure_emits_internal_error_without_committing_turn(
    ctx: chat_runtime_tests._Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_token_counter(**_kwargs: object) -> int:
        raise RuntimeError("force byte-count fallback")

    fake_litellm = types.SimpleNamespace(
        token_counter=_raise_token_counter, get_model_info=_raise_token_counter
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    restore_calls = 0

    async def _fail_restore(_sink: AuditSink, _events: object) -> None:
        nonlocal restore_calls
        restore_calls += 1
        raise RuntimeError("recovery database unavailable")

    monkeypatch.setattr(AuditSink, "restore_context_rejected", _fail_restore)

    passage = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="large.pdf",
        ord=0,
        text="E" * 8_000,
        char_start=0,
        char_end=8_000,
        score=0.9,
    )
    gateway = chat_runtime_tests._RecordingSearchGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid4().hex
    runtime = chat_runtime_tests._runtime(
        ctx,
        gateway=gateway,
        retrieval=chat_runtime_tests._FakeRetrieval([passage]),
        backplane=backplane,
        context_config=ContextConfig(fallback_max_input_tokens=9_072, output_headroom_tokens=0),
    )
    consumer = asyncio.create_task(chat_runtime_tests._drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="summarize the large document",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
    )
    events = await asyncio.wait_for(consumer, timeout=2)

    terminals = [event for event in events if event["type"] in {"done", "error"}]
    assert len(terminals) == 1
    assert terminals[0]["type"] == "error"
    assert terminals[0]["problem"]["code"] == "internal_error"  # type: ignore[index]
    assert restore_calls == 1

    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        invocations = await ToolInvocationRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        audits = await AuditEventRepository(session, ctx.tenant_id).list_recent(limit=100)
    assert not [message for message in messages if message.role.value == "assistant"]
    assert invocations == []
    assert audits == []


async def test_simulated_context_rejection_skips_recovery_commit(
    ctx: chat_runtime_tests._Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_token_counter(**_kwargs: object) -> int:
        raise RuntimeError("force byte-count fallback")

    fake_litellm = types.SimpleNamespace(
        token_counter=_raise_token_counter, get_model_info=_raise_token_counter
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    restore_calls = 0

    async def _fail_if_restored(_sink: AuditSink, _events: object) -> None:
        nonlocal restore_calls
        restore_calls += 1
        raise AssertionError("simulated runs must not recover context audit writes")

    monkeypatch.setattr(AuditSink, "restore_context_rejected", _fail_if_restored)

    passage = RetrievedPassage(
        chunk_id=ctx.chunk_id,
        document_id=ctx.document_id,
        document_name="large.pdf",
        ord=0,
        text="S" * 8_000,
        char_start=0,
        char_end=8_000,
        score=0.9,
    )
    backplane = InMemoryBackplane()
    stream_id = uuid4().hex
    runtime = chat_runtime_tests._runtime(
        ctx,
        gateway=chat_runtime_tests._RecordingSearchGateway(),
        retrieval=chat_runtime_tests._FakeRetrieval([passage]),
        backplane=backplane,
        context_config=ContextConfig(fallback_max_input_tokens=9_072, output_headroom_tokens=0),
    )
    consumer = asyncio.create_task(chat_runtime_tests._drain(backplane, stream_id))
    await asyncio.sleep(0)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="summarize the large document",
        model="some/unknown-model-not-in-map",
        history=[],
        collection_ids=None,
        simulate_writes=True,
    )
    events = await asyncio.wait_for(consumer, timeout=2)

    assert any(event.get("name") == "tool_call" for event in events)
    assert any(event.get("name") == "tool_result" for event in events)
    terminals = [event for event in events if event["type"] in {"done", "error"}]
    assert len(terminals) == 1
    assert terminals[0]["type"] == "error"
    assert terminals[0]["problem"]["code"] == "context_too_large"  # type: ignore[index]
    assert restore_calls == 0

    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        invocations = await ToolInvocationRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        audits = await AuditEventRepository(session, ctx.tenant_id).list_recent(limit=100)
    assert not [message for message in messages if message.role.value == "assistant"]
    assert invocations == []
    assert audits == []
