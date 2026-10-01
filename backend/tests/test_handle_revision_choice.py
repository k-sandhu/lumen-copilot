"""A cited handle selects the exact revision observed during one answer."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from sqlalchemy import update

from app.db import models
from app.db.repositories import CitationRepository, MessageRepository
from app.domain.llm import StreamEvent, ToolCall
from app.domain.retrieval import RetrievedPassage
from app.realtime.backplane import InMemoryBackplane
from app.services.prompts.grounded_answer import NO_SOURCES_FALLBACK
from tests.test_chat_handle_contract import _visible_answer
from tests.test_chat_runtime import _Ctx, _drain, _FakeRetrieval, _passage, _runtime

pytest_plugins = ("tests.test_chat_runtime",)


class _Revisions(_FakeRetrieval):
    def __init__(self, ctx: _Ctx) -> None:
        first = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
        super().__init__([first])
        self._ctx = ctx
        self.current = replace(first, text=first.text.replace("14,600", "15,600"))

    async def search_text(self, **kwargs: object) -> list[RetrievedPassage]:
        if self.queries:
            async with self._ctx.sessionmaker() as session:
                await session.execute(
                    update(models.Chunk)
                    .where(models.Chunk.id == self._ctx.chunk_id)
                    .values(text=self.current.text)
                )
                await session.commit()
            self._passages = [self.current]
        return await super().search_text(**kwargs)  # type: ignore[arg-type]


class _RevisionGateway:
    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.calls = 0
        self.selected: list[str] = []

    async def stream_tools(self, messages: object, **kwargs: object) -> AsyncIterator[StreamEvent]:
        del kwargs
        if self.calls < 2:
            self.calls += 1
            yield StreamEvent(
                tool_calls=(
                    ToolCall(
                        id=f"search-{self.calls}",
                        name="search_text",
                        arguments={"query": f"deduction revision {self.calls}"},
                    ),
                ),
                finish_reason="tool_calls",
            )
            return
        tools = [
            message.content
            for message in messages  # type: ignore[union-attr]
            if message.role.value == "tool"
        ]
        handles = [re.search(r"\[(S[0-9]+)\]", text).group(1) for text in tools]
        assert len(handles) == 2 and handles[0] != handles[1]
        self.selected = handles if self.choice == "both" else [handles[self.choice == "current"]]
        yield StreamEvent(
            text="The current deduction is $15,600 "
            + " ".join(f"[{handle}]" for handle in self.selected)
            + "."
        )
        yield StreamEvent(finish_reason="stop")


@pytest.mark.parametrize("choice", ["current", "stale", "both"])
async def test_cited_handle_selects_current_revision_or_refuses_stale_evidence(
    ctx: _Ctx,
    choice: str,
) -> None:
    gateway = _RevisionGateway(choice)
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(ctx, gateway=gateway, retrieval=_Revisions(ctx), backplane=backplane)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What is the deduction?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    events = await _drain(backplane, stream_id)
    assert not any(event["type"] == "error" for event in events)
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        answer = next(message for message in messages if message.role.value == "assistant")
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message_hydrated(
            answer.id
        )
    assert _visible_answer(events) == answer.content
    if choice == "current":
        assert len(citations) == 1
        assert citations[0].handle == gateway.selected[0]
        assert "$15,600" in citations[0].snippet
        assert f"[{gateway.selected[0]}]" in answer.content
    else:
        assert answer.content == NO_SOURCES_FALLBACK
        assert citations == []
