"""Durable allocation must retain the coordinator's session and tenant binding."""

# Imported fixture intentionally shares its injected parameter name.
# ruff: noqa: F811

import asyncio
from uuid import uuid4

import pytest

import app.services.chat_runtime as chat_runtime
from app.domain.llm import StreamEvent
from app.realtime.backplane import InMemoryBackplane
from tests.test_chat_runtime import _drain, _FakeRetrieval, _runtime, _ScriptedGateway
from tests.test_chat_runtime import ctx as ctx


async def test_reservation_commits_and_rebinds_the_same_coordinator_session(
    ctx: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int]] = []
    original_bind = chat_runtime.bind_tenant

    def factory():  # noqa: ANN202
        session = ctx.sessionmaker()  # type: ignore[attr-defined]
        original_commit = session.commit

        async def commit() -> None:
            events.append(("commit", id(session)))
            await original_commit()

        session.commit = commit
        return session

    async def bind(session: object, tenant: object) -> None:
        events.append(("bind", id(session)))
        await original_bind(session, tenant)  # type: ignore[arg-type]

    monkeypatch.setattr(chat_runtime, "bind_tenant", bind)
    backplane = InMemoryBackplane()
    stream_id = uuid4().hex
    consumer = asyncio.create_task(_drain(backplane, stream_id))
    await asyncio.sleep(0)
    runtime = _runtime(
        ctx,  # type: ignore[arg-type]
        gateway=_ScriptedGateway([[StreamEvent(text="I cannot answer from sources.")]]),
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
        sessionmaker=factory,
        tool_concurrency=1,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="q",  # type: ignore[attr-defined]
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    envelopes = await consumer
    assert envelopes[-1]["type"] == "done"
    coordinator = events[0][1]
    assert events[:3] == [("bind", coordinator), ("commit", coordinator), ("bind", coordinator)]
