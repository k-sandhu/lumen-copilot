"""Route-level handle validation with current permission reads and audit readback."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytest_plugins = ("tests.test_chat_api",)


def _visible_answer(envs: list[dict[str, object]]) -> str:
    answer = ""
    for env in envs:
        if env["type"] == "delta":
            data = env["data"]
            assert isinstance(data, dict)
            answer += str(data["text"])
        elif env["type"] == "event" and env.get("name") == "answer_retract":
            answer = ""
    return answer


def _install_real_permission_read(
    monkeypatch: pytest.MonkeyPatch,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    answer: str,
    revoke_before_answer: bool = False,
) -> None:
    from app.domain.llm import StreamEvent, ToolCall
    from app.retrieval.service import RetrievalService
    from tests import test_chat_api

    async def read_passages(
        self: object,
        *,
        principal: object,
        chunk_ids: list[UUID],
        collection_ids: object = None,
        document_ids: object = None,
    ) -> list[object]:
        del self
        async with sessionmaker() as session:
            service = RetrievalService(session, gateway=object())  # type: ignore[arg-type]
            return cast(
                list[object],
                await service.read_passages(
                    principal=principal,  # type: ignore[arg-type]
                    chunk_ids=chunk_ids,
                    collection_ids=collection_ids,  # type: ignore[arg-type]
                    document_ids=document_ids,  # type: ignore[arg-type]
                ),
            )

    async def stream_tools(
        self: object, messages: object, **kwargs: object
    ) -> AsyncIterator[StreamEvent]:
        del self, kwargs
        transcript = list(messages)  # type: ignore[arg-type]
        has_tool_result = any(getattr(m, "role", None).value == "tool" for m in transcript)
        if not has_tool_result:
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="route-search", name="search_text", arguments={"query": "fact"}),
                ),
                finish_reason="tool_calls",
            )
            return
        if revoke_before_answer:
            from app.db import models

            async with sessionmaker() as session:
                await session.execute(
                    update(models.Document)
                    .where(models.Document.id == test_chat_api_seeded.alice_doc)
                    .values(acl_enforced=True, acl_synced_at=None)
                )
                await session.commit()
        yield StreamEvent(text=answer)
        yield StreamEvent(finish_reason="stop")

    # Bound at test call time to the seeded document for this route fixture.
    from tests.test_chat_api import _Seeded

    test_chat_api_seeded = cast(_Seeded, sessionmaker.lumen_seeded)  # type: ignore[attr-defined]
    monkeypatch.setattr(test_chat_api._FakeRetrieval, "read_passages", read_passages, raising=False)
    monkeypatch.setattr(test_chat_api._ScriptedGateway, "stream_tools", stream_tools)


async def _send_and_collect(
    client: AsyncClient,
    backplane: object,
    seeded: object,
    *,
    content: str,
) -> tuple[str, list[dict[str, object]], dict[str, object]]:
    from tests.test_chat_api import _PASSWORD, _auth

    email = str(seeded.alice_email)  # type: ignore[attr-defined]
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    headers = _auth(str(login.json()["access_token"]))
    created = await client.post("/api/v1/chat/sessions", headers=headers, json={})
    assert created.status_code == 201, created.text
    session_id = str(created.json()["id"])
    sent = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        headers=headers,
        json={"content": content},
    )
    assert sent.status_code == 202, sent.text
    stream_id = str(sent.json()["stream_id"])
    events = [event async for event in backplane.subscribe(stream_id)]  # type: ignore[attr-defined]
    history = await client.get(f"/api/v1/chat/sessions/{session_id}/messages", headers=headers)
    assert history.status_code == 200, history.text
    return session_id, events, history.json()


@pytest.mark.asyncio
async def test_route_refusal_after_retrieval_has_no_citations(
    app: object,
    client: AsyncClient,
    backplane: object,
    seeded: object,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import models
    from app.db.models import Message
    from app.db.repositories import AuditEventRepository
    from app.services.prompts.grounded_answer import NO_SOURCES_FALLBACK

    _install_real_permission_read(monkeypatch, sessionmaker, answer=NO_SOURCES_FALLBACK)
    session_id, events, history = await _send_and_collect(
        client, backplane, seeded, content="What is the fact?"
    )

    done = next(event for event in events if event["type"] == "done")
    assert done["data"]["citationCount"] == 0  # type: ignore[index]
    assistants = [item for item in history["items"] if item["role"] == "assistant"]  # type: ignore[index]
    assert len(assistants) == 1
    assert assistants[0]["content"] == NO_SOURCES_FALLBACK
    assert assistants[0]["citations"] == []
    async with sessionmaker() as after:
        handles = list(
            (
                await after.execute(
                    select(models.SourceHandle).where(
                        models.SourceHandle.session_id == UUID(session_id)
                    )
                )
            ).scalars()
        )
        citations = list(
            (
                await after.execute(
                    select(models.Citation)
                    .join(Message, Message.id == models.Citation.message_id)
                    .where(Message.session_id == UUID(session_id))
                )
            ).scalars()
        )
        web_citations = list(
            (
                await after.execute(
                    select(models.WebCitation)
                    .join(Message, Message.id == models.WebCitation.message_id)
                    .where(Message.session_id == UUID(session_id))
                )
            ).scalars()
        )
        audit = await AuditEventRepository(after, seeded.tenant_a).list_recent(  # type: ignore[attr-defined]
            limit=50
        )
    assert handles
    assert all("text" not in handle.evidence for handle in handles)
    assert "2024 standard" not in str([handle.evidence for handle in handles])
    assert citations == []
    assert web_citations == []
    retrieval = [event for event in audit if event.action == "retrieval.query"]
    answer = [event for event in audit if event.action == "answer.generated"]
    assert retrieval and answer
    assert answer[0].metadata["citation_count"] == 0


@pytest.mark.asyncio
async def test_route_rechecks_current_permission_before_persisting_a_handle_citation(
    app: object,
    client: AsyncClient,
    backplane: object,
    seeded: object,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import models
    from app.db.models import Message
    from app.db.repositories import AuditEventRepository
    from app.services.prompts.grounded_answer import NO_SOURCES_FALLBACK

    _install_real_permission_read(
        monkeypatch,
        sessionmaker,
        answer="The answer was 42 [S1].",
        revoke_before_answer=True,
    )
    session_id, events, history = await _send_and_collect(
        client, backplane, seeded, content="What is the answer?"
    )

    done = next(event for event in events if event["type"] == "done")
    assert done["data"]["citationCount"] == 0  # type: ignore[index]
    assert _visible_answer(events) == NO_SOURCES_FALLBACK
    assistants = [item for item in history["items"] if item["role"] == "assistant"]  # type: ignore[index]
    assert len(assistants) == 1
    assert assistants[0]["content"] == NO_SOURCES_FALLBACK
    assert assistants[0]["citations"] == []
    async with sessionmaker() as after:
        handles = list(
            (
                await after.execute(
                    select(models.SourceHandle).where(
                        models.SourceHandle.session_id == UUID(session_id)
                    )
                )
            ).scalars()
        )
        citations = list(
            (
                await after.execute(
                    select(models.Citation)
                    .join(Message, Message.id == models.Citation.message_id)
                    .where(Message.session_id == UUID(session_id))
                )
            ).scalars()
        )
        web_citations = list(
            (
                await after.execute(
                    select(models.WebCitation)
                    .join(Message, Message.id == models.WebCitation.message_id)
                    .where(Message.session_id == UUID(session_id))
                )
            ).scalars()
        )
        audit = await AuditEventRepository(after, seeded.tenant_a).list_recent(  # type: ignore[attr-defined]
            limit=50
        )
    assert handles
    assert all("text" not in handle.evidence for handle in handles)
    assert "2024 standard" not in str([handle.evidence for handle in handles])
    assert citations == []
    assert web_citations == []
    answer = [event for event in audit if event.action == "answer.generated"]
    assert answer and answer[0].metadata["citation_count"] == 0
    rehydration = [
        event
        for event in audit
        if event.action == "retrieval.evidence_rehydrated"
        and event.metadata.get("requested_passages") == 1
    ]
    assert rehydration
    assert rehydration[0].metadata["permitted_passages"] == 0
