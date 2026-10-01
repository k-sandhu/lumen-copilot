"""Route proof for durable tool traces when context rejects the answer turn."""

from __future__ import annotations

import sys
import types
from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.retrieval import RetrievedPassage
from tests import test_chat_api as chat_fixtures
from tests.test_chat_api import _Seeded as _Seeded

app = chat_fixtures.app
backplane = chat_fixtures.backplane
client = chat_fixtures.client
seeded = chat_fixtures.seeded
sessionmaker = chat_fixtures.sessionmaker


class _SearchThenStop:
    def __init__(self) -> None:
        self.calls = 0

    async def stream_tools(self, messages: object, **kwargs: object) -> AsyncIterator[object]:
        del messages, kwargs
        from app.domain.llm import StreamEvent, ToolCall

        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="context-search", name="search_text", arguments={"query": "fact"}),
                ),
                finish_reason="tool_calls",
            )
        else:
            yield StreamEvent(text="unexpected over-budget provider request")
            yield StreamEvent(finish_reason="stop")


@pytest.mark.parametrize(
    "stage_document", [False, True], ids=["no-staged-write", "rollback-staged-write"]
)
@pytest.mark.asyncio
async def test_context_refusal_preserves_read_audit_without_failed_turn_writes(
    stage_document: bool,
    app: FastAPI,
    client: AsyncClient,
    backplane: object,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.deps import get_settings_dep
    from app.core.config import get_settings
    from app.db import models
    from app.db.repositories import AuditEventRepository, ToolInvocationRepository
    from app.domain.llm import ChatMessage, Role
    from app.llm.context import estimate_message_tokens
    from app.services.chat_runtime import ChatRuntime
    from app.services.prompts.grounded_answer import GROUNDED_SYSTEM_PROMPT
    from app.services.tools.registry import default_allowlist, tool_specs
    from tests import test_chat_api

    def byte_counter(text: str) -> int:
        return len(text.encode("utf-8"))

    def unavailable(**_kwargs: object) -> int:
        raise RuntimeError("use deterministic byte counter")

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        types.SimpleNamespace(token_counter=unavailable, get_model_info=unavailable),
    )
    monkeypatch.setattr(ChatRuntime, "_token_counter_for", lambda self, model: byte_counter)

    fixed_cost = estimate_message_tokens(
        [
            ChatMessage(role=Role.SYSTEM, content=GROUNDED_SYSTEM_PROMPT),
            ChatMessage(role=Role.USER, content="fact"),
        ],
        tool_specs(default_allowlist()),
        counter=byte_counter,
    )
    # Context's fixed safety margin is 1024. The initial prompt gets exactly
    # 512 bytes of additional room, so the 32K retrieved passage must be refused.
    budget = fixed_cost + 512
    settings = get_settings().model_copy(
        update={
            "context_fallback_max_input_tokens": budget + 1024,
            "context_output_headroom_tokens": 0,
        }
    )
    app.dependency_overrides[get_settings_dep] = lambda: settings

    passage = RetrievedPassage(
        chunk_id=seeded.alice_chunk,
        document_id=seeded.alice_doc,
        document_name="taxes.pdf",
        ord=0,
        text="P" * 32_000,
        char_start=0,
        char_end=32_000,
        score=0.9,
    )
    staged_name = f"must-rollback-{UUID(int=1)}.pdf"

    async def search_with_optional_stage(self: object, **kwargs: object) -> list[RetrievedPassage]:
        del kwargs
        if stage_document:
            session = cast(AsyncSession, self._session)
            collection_id = await session.scalar(
                select(models.Collection.id).where(
                    models.Collection.tenant_id == seeded.tenant_a,
                    models.Collection.owner_id == seeded.alice_id,
                )
            )
            assert collection_id is not None
            session.add(
                models.Document(
                    tenant_id=seeded.tenant_a,
                    owner_id=seeded.alice_id,
                    collection_id=collection_id,
                    filename=staged_name,
                    mime_type="application/pdf",
                    size_bytes=1,
                    storage_key=f"{seeded.tenant_a}/{staged_name}",
                    status="ready",
                    acl_enforced=False,
                )
            )
            await session.flush()
        return [passage]

    monkeypatch.setattr(test_chat_api._FakeRetrieval, "search_text", search_with_optional_stage)
    # The app fixture's fake retrieval is shared with the runtime; give it only
    # the runtime-owned session so a staged write participates in that transaction.
    original_init = ChatRuntime.__init__

    def init_with_session(self: ChatRuntime, **kwargs: object) -> None:
        retrieval_factory = cast(object, kwargs["retrieval_factory"])

        def bind_runtime_session(session: AsyncSession) -> object:
            retrieval = retrieval_factory(session)  # type: ignore[operator]
            retrieval._session = session
            return retrieval

        kwargs["retrieval_factory"] = bind_runtime_session
        original_init(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ChatRuntime, "__init__", init_with_session)
    gateway = _SearchThenStop()
    monkeypatch.setattr(test_chat_api.chat_module, "get_llm_gateway", lambda: gateway)

    token = await test_chat_api._login(client, seeded.alice_email)
    headers = test_chat_api._auth(token)
    created = await client.post("/api/v1/chat/sessions", headers=headers, json={})
    assert created.status_code == 201, created.text
    chat_id = UUID(str(created.json()["id"]))
    sent = await client.post(
        f"/api/v1/chat/sessions/{chat_id}/messages",
        headers=headers,
        json={"content": "fact"},
    )
    assert sent.status_code == 202, sent.text
    events = [event async for event in backplane.subscribe(str(sent.json()["stream_id"]))]  # type: ignore[attr-defined]

    tool_results = [
        event
        for event in events
        if event.get("type") == "event" and event.get("name") == "tool_result"
    ]
    assert tool_results, events
    assert tool_results[0]["data"]["ok"] is True  # type: ignore[index]
    assert tool_results[0]["data"]["hitCount"] == 1  # type: ignore[index]
    assert events[-1]["type"] == "error"
    assert events[-1]["problem"]["code"] == "context_too_large"  # type: ignore[index]
    assert gateway.calls == 1

    history = await client.get(f"/api/v1/chat/sessions/{chat_id}/messages", headers=headers)
    assert history.status_code == 200, history.text
    history_items = history.json()["items"]
    assert all(item["role"] != "assistant" for item in history_items)
    assert all(not item["citations"] for item in history_items)

    async with sessionmaker() as after:
        audit = await AuditEventRepository(after, seeded.tenant_a).list_recent(limit=100)
        invocations = await ToolInvocationRepository(after, seeded.tenant_a).list_for_session(
            chat_id
        )
        staged = await after.scalar(
            select(models.Document).where(
                models.Document.tenant_id == seeded.tenant_a,
                models.Document.filename == staged_name,
            )
        )

    actions = {event.action for event in audit}
    required = {"retrieval.query", "tool.invoked", "tool.result"}
    if stage_document:
        assert staged is None, "the staged document survived the refused assistant transaction"
    missing = sorted(required - actions)
    if not invocations:
        missing.append("tool invocation")
    assert not missing, f"completed tool trace was not durable after refusal: {missing}"
    assert len(invocations) == 1
    assert invocations[0].tool_name == "search_text"
    assert invocations[0].ok is True
    assert invocations[0].message_id is None
