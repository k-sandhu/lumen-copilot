"""Canonical reads retain navigation and SQL media evidence through the provider loop."""

# ruff: noqa: F811
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import update

from app.db import models
from app.db.repositories import (
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CitationRepository,
    MessageRepository,
)
from app.domain.entities import DocumentKind, DocumentStatus
from app.domain.llm import Role as LlmRole
from app.domain.llm import StreamEvent, ToolCall
from app.realtime.backplane import InMemoryBackplane
from app.retrieval import RetrievalService
from app.services.tools.handles import EvidenceHandles
from app.services.tools.registry import get_tool
from app.services.tools.types import ToolContext
from tests.test_chat_runtime import _Ctx, _runtime
from tests.test_chat_runtime import ctx as ctx
from tests.test_handle_real_media_reads import _seed_media


class _ReadGateway:
    def __init__(self, arguments: dict[str, object]) -> None:
        self.arguments = arguments
        self.tool_messages: list[str] = []

    async def stream_tools(
        self, messages: Any, *, tools: object, **kwargs: object
    ) -> AsyncIterator[StreamEvent]:
        replies = [message.content for message in messages if message.role is LlmRole.TOOL]
        if not replies:
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="range-read", name="read_document", arguments=self.arguments),
                ),
                finish_reason="tool_calls",
            )
        else:
            self.tool_messages = replies
            yield StreamEvent(text="The source supports this answer. [S1]")
            yield StreamEvent(finish_reason="stop")


async def _run_read(ctx: _Ctx, arguments: dict[str, object]) -> tuple[_ReadGateway, list[Any]]:
    gateway = _ReadGateway(arguments)
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=None,
        retrieval_factory=lambda session: RetrievalService(session, gateway=None),
        backplane=backplane,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="Read the source",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    # Replay after producer completion is a deterministic terminal handshake.
    return gateway, [event async for event in backplane.subscribe(stream_id)]


async def test_provider_transcript_keeps_truncated_read_range_and_continuation(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        await session.execute(
            update(models.Document)
            .where(models.Document.id == ctx.document_id)
            .values(status=DocumentStatus.READY.value)
        )
        await ChunkRepository(session, ctx.tenant_id).replace_for_document(
            ctx.document_id,
            [
                ChunkInput(text="first passage", char_start=0, char_end=13),
                ChunkInput(text="second passage", char_start=13, char_end=27),
            ],
        )
        await session.commit()
    gateway, events = await _run_read(ctx, {"document": str(ctx.document_id), "max_passages": 1})
    assert events[-1]["type"] == "done"
    content = "\n".join(gateway.tool_messages)
    assert "[S1] taxes.pdf\nfirst passage" in content
    assert "Available range: 0-27; returned 0-13." in content
    assert "continue read_document(document=D2, start=13, end=null)" in content
    assert "second passage" not in content


@pytest.mark.parametrize("kind", [DocumentKind.AUDIO, DocumentKind.VIDEO])
@pytest.mark.parametrize("selector", ["document", "around"])
async def test_sql_range_and_around_reads_preserve_media_identity(
    ctx: _Ctx, kind: DocumentKind, selector: str
) -> None:
    async with ctx.sessionmaker() as session:
        owner, _, passage = await _seed_media(session, kind=kind)
        handles = EvidenceHandles()
        handle = handles.passage(passage)
        arguments = (
            {"document": str(passage.document_id)} if selector == "document" else {"around": handle}
        )
        result = await get_tool("read_document").handler(
            arguments,
            ToolContext(
                principal=owner, retrieval=RetrievalService(session, gateway=None), handles=handles
            ),
        )
        assert result.ok and len(result.passages) == 1
        assert handles.passage(result.passages[0]) == handle
        assert result.passages[0].speaker_name == "Jordan"
        assert result.passages[0].transcript_segment_id == passage.transcript_segment_id


@pytest.mark.parametrize("kind", [DocumentKind.AUDIO, DocumentKind.VIDEO])
@pytest.mark.parametrize("selector", ["document", "around"])
async def test_sql_media_read_citation_persists_after_final_rehydration(
    ctx: _Ctx, kind: DocumentKind, selector: str
) -> None:
    from app.db.evidence_handles import HandleRepository

    async with ctx.sessionmaker() as session:
        owner, _, passage = await _seed_media(session, kind=kind)
        chat = await ChatSessionRepository(session, owner.tenant_id).create(
            owner_id=owner.user_id, model="anthropic/claude-opus-4.8", title="Media read"
        )
        if selector == "around":
            handles = EvidenceHandles()
            handles.passage(passage)
            await HandleRepository(session, owner, chat.id).append(handles.entries)
        await session.commit()
    media_ctx = _Ctx(
        sessionmaker=ctx.sessionmaker,
        principal=owner,
        session_id=chat.id,
        document_id=passage.document_id,
        chunk_id=passage.chunk_id,
        tenant_id=owner.tenant_id,
    )
    arguments = (
        {"document": str(passage.document_id)} if selector == "document" else {"around": "S1"}
    )
    gateway, events = await _run_read(media_ctx, arguments)
    assert gateway.tool_messages
    assert "time 00:01.250-00:03.400" in "\n".join(gateway.tool_messages)
    assert "Jordan" in "\n".join(gateway.tool_messages)
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["citationCount"] == 1
    async with ctx.sessionmaker() as readback:
        messages = await MessageRepository(readback, owner.tenant_id).list_for_session(chat.id)
        answer = next(message for message in messages if message.role.value == "assistant")
        citations = await CitationRepository(readback, owner.tenant_id).list_for_message_hydrated(
            answer.id
        )
        assert answer.source_document_ids == (passage.document_id,)
        assert len(citations) == 1
        assert citations[0].handle == "S1"
        assert citations[0].time_start_ms == 1_250
        assert citations[0].time_end_ms == 3_400
        assert citations[0].speaker_name == "Jordan"
