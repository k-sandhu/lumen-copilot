"""Canonical corpus tools keep retrieval audit events on the chat route."""

# Imported fixtures intentionally share names with injected test parameters.
# ruff: noqa: F811

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_chat_api import app as app
from tests.test_chat_api import backplane as backplane
from tests.test_chat_api import client as client
from tests.test_chat_api import seeded as seeded
from tests.test_chat_api import sessionmaker as sessionmaker
from tests.test_chat_handle_routes import _send_and_collect


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("search_passages", {"query": "fact"}),
        ("find_documents", {"query": "taxes"}),
        ("read_document", {"document": "seeded-document"}),
    ],
    ids=["search-passages", "find-documents", "read-document"],
)
async def test_canonical_corpus_tools_emit_committed_retrieval_audit(
    app: FastAPI,
    client: AsyncClient,
    backplane: object,
    seeded: object,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    from app.domain.llm import StreamEvent, ToolCall
    from app.retrieval.service import RetrievalService
    from app.services.prompts.grounded_answer import NO_SOURCES_FALLBACK
    from tests import test_chat_api

    if tool_name == "read_document":
        arguments = {"document": str(seeded.alice_doc)}  # type: ignore[attr-defined]

    async def find_documents(
        self: object,
        *,
        principal: object,
        **kwargs: object,
    ) -> object:
        service = RetrievalService(
            cast(AsyncSession, self._session),
            gateway=None,  # type: ignore[attr-defined,arg-type]
        )
        return await service.find_documents(
            principal=principal,  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )

    async def read_document(
        self: object,
        *,
        principal: object,
        document_id: UUID,
        start: int = 0,
        end: int | None = None,
        max_passages: int = 5,
        collection_ids: list[UUID] | None = None,
        document_ids: list[UUID] | None = None,
    ) -> object:
        service = RetrievalService(
            cast(AsyncSession, self._session),
            gateway=None,  # type: ignore[attr-defined,arg-type]
        )
        return await service.read_document(
            principal=principal,  # type: ignore[arg-type]
            document_id=document_id,
            start=start,
            end=end,
            max_passages=max_passages,
            collection_ids=collection_ids,
            document_ids=document_ids,
        )

    class RefusingAfterToolGateway:
        async def stream_tools(
            self,
            messages: object,
            *,
            tools: object,
            model: object = None,
            tool_choice: object = None,
            api_key: object = None,
            api_base: object = None,
            cache_key: object = None,
            max_tokens: object = None,
        ) -> AsyncIterator[StreamEvent]:
            del tools, model, tool_choice, api_key, api_base, cache_key, max_tokens
            transcript = list(messages)  # type: ignore[arg-type]
            has_tool_result = any(
                getattr(getattr(message, "role", None), "value", None) == "tool"
                for message in transcript
            )
            if not has_tool_result:
                yield StreamEvent(
                    tool_calls=(
                        ToolCall(id="canonical-audit", name=tool_name, arguments=arguments),
                    ),
                    finish_reason="tool_calls",
                )
                return
            yield StreamEvent(text=NO_SOURCES_FALLBACK)
            yield StreamEvent(finish_reason="stop")

    monkeypatch.setattr(
        test_chat_api._FakeRetrieval, "find_documents", find_documents, raising=False
    )
    monkeypatch.setattr(test_chat_api._FakeRetrieval, "read_document", read_document, raising=False)
    monkeypatch.setattr(
        test_chat_api.chat_module, "get_llm_gateway", lambda: RefusingAfterToolGateway()
    )

    from tests.test_chat_api import _Seeded

    typed_seeded = cast(_Seeded, seeded)
    session_id, events, history = await _send_and_collect(
        client, backplane, seeded, content="Look up the source, but do not cite it."
    )

    done = next(event for event in events if event["type"] == "done")
    assert done["data"]["citationCount"] == 0  # type: ignore[index]
    assistant_messages = [
        message
        for message in history["items"]
        if message["role"] == "assistant"  # type: ignore[index]
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] == NO_SOURCES_FALLBACK
    assert assistant_messages[0]["citations"] == []

    from app.db.repositories import AuditEventRepository, ToolInvocationRepository

    async with sessionmaker() as after:
        audit = await AuditEventRepository(after, typed_seeded.tenant_a).list_recent(limit=100)
        invocations = await ToolInvocationRepository(after, typed_seeded.tenant_a).list_for_session(
            UUID(session_id)
        )
    assert len(invocations) == 1
    assert invocations[0].ok is True, invocations[0].error
    assert invocations[0].result_summary
    retrieval = [
        event
        for event in audit
        if event.action == "retrieval.query" and event.metadata.get("tool") == tool_name
    ]
    assert retrieval, f"missing retrieval.query audit event for {tool_name}"
    assert retrieval[0].metadata["hit_count"] > 0
    assert any(
        event.action == "tool.invoked" and event.metadata.get("tool") == tool_name
        for event in audit
    )
    assert any(
        event.action == "tool.result" and event.metadata.get("tool") == tool_name for event in audit
    )
    answers = [event for event in audit if event.action == "answer.generated"]
    assert answers
    assert answers[0].metadata["citation_count"] == 0
