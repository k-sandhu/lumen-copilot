"""PR #757 R2: audit recovery, document navigation and literal Markdown."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.db import models
from app.db.evidence_handles import HandleRepository
from app.db.repositories import AuditEventRepository, ChatSessionRepository, MessageRepository
from app.domain.entities import AuditEvent
from app.domain.llm import ChatMessage, Role, StreamEvent, ToolCall
from app.llm.context import ContextConfig, estimate_message_tokens
from app.realtime.backplane import InMemoryBackplane
from app.retrieval import RetrievalService
from app.services.audit import AuditSink
from app.services.chat_runtime import ChatRuntime
from app.services.prompts.grounded_answer import GROUNDED_SYSTEM_PROMPT
from app.services.tools.handles import select_cited_handles
from app.services.tools.registry import default_allowlist, tool_specs
from tests import test_chat_runtime as runtime_tests
from tests.test_chat_handle_contract import _run_answer, _visible_answer

ctx = runtime_tests.ctx


async def test_followup_context_rejection_restores_only_uncommitted_read_events(
    ctx: runtime_tests._Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    def byte_counter(text: str) -> int:
        return len(text.encode("utf-8"))

    monkeypatch.setattr(ChatRuntime, "_token_counter_for", lambda self, model: byte_counter)
    fixed = estimate_message_tokens(
        [
            ChatMessage(role=Role.SYSTEM, content=GROUNDED_SYSTEM_PROMPT),
            ChatMessage(role=Role.USER, content="fact"),
        ],
        tool_specs(default_allowlist()),
        counter=byte_counter,
    )
    passage = replace(
        runtime_tests._passage(ctx.document_id, ctx.chunk_id, "taxes.pdf"),
        text="P" * 32_000,
        char_start=0,
        char_end=32_000,
    )
    gateway = runtime_tests._ScriptedGateway(
        [[StreamEvent(tool_calls=(ToolCall("search", "search_text", {"query": "fact"}),))]]
    )
    backplane = InMemoryBackplane()
    runtime = runtime_tests._runtime(
        ctx,
        gateway=gateway,
        retrieval=runtime_tests._FakeRetrieval([passage]),
        backplane=backplane,
        context_config=ContextConfig(
            fallback_max_input_tokens=fixed + 512 + 1024, output_headroom_tokens=0
        ),
    )
    restored: list[AuditEvent] = []
    original_restore = AuditSink.restore_context_rejected

    async def capture_restore(sink: AuditSink, events: Sequence[AuditEvent]) -> None:
        restored.extend(events)
        await original_restore(sink, events)

    monkeypatch.setattr(AuditSink, "restore_context_rejected", capture_restore)
    stream_id = uuid.uuid4().hex
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="fact",
        model="test/model",
        history=[],
        collection_ids=None,
        evidence=[(ctx.document_id, ctx.chunk_id)],
    )
    events = await runtime_tests._drain(backplane, stream_id)
    assert events[-1]["type"] == "error"
    assert events[-1]["problem"]["code"] == "context_too_large"
    assert gateway.calls == 1
    async with ctx.sessionmaker() as after:
        audits = await AuditEventRepository(after, ctx.tenant_id).list_recent(limit=100)
        invocations = (await after.scalars(select(models.ToolInvocation))).all()
        messages = await MessageRepository(after, ctx.tenant_id).list_for_session(ctx.session_id)
    required = {"retrieval.evidence_rehydrated", "retrieval.query", "tool.invoked", "tool.result"}
    assert {event.action for event in audits} == required
    assert len(audits) == 4
    assert {event.id for event in restored} == {event.id for event in audits}
    assert len(invocations) == 1 and invocations[0].ok is True
    assert invocations[0].message_id is None
    assert not [message for message in messages if message.role.value == "assistant"]


class _DocumentGateway:
    def __init__(self, discovery: str | None = None, handle: str = "D1") -> None:
        self.discovery = discovery
        self.handle = handle
        self.calls = 0
        self.replies: list[str] = []

    async def stream_tools(
        self, messages: Sequence[ChatMessage], **kwargs: object
    ) -> AsyncIterator[StreamEvent]:
        del kwargs
        self.replies = [message.content for message in messages if message.role == Role.TOOL]
        self.calls += 1
        if self.discovery and self.calls == 1:
            args = {"name_or_query": "taxes"} if self.discovery == "search_documents" else {}
            yield StreamEvent(tool_calls=(ToolCall("discover", self.discovery, args),))
        elif self.calls == (2 if self.discovery else 1):
            if self.discovery:
                assert "taxes.pdf (id: D1)" in self.replies[-1]
            yield StreamEvent(
                tool_calls=(ToolCall("read", "get_document", {"document_id": self.handle}),)
            )
        else:
            yield StreamEvent(text="Finished.", finish_reason="stop")


async def _document_turn(
    ctx: runtime_tests._Ctx, gateway: _DocumentGateway, session_id: uuid.UUID | None = None
) -> list[dict[str, object]]:
    backplane = InMemoryBackplane()
    runtime = runtime_tests._runtime(
        ctx,
        gateway=gateway,
        retrieval=None,
        retrieval_factory=lambda session: RetrievalService(session, gateway=gateway),
        backplane=backplane,
    )
    stream_id = uuid.uuid4().hex
    await runtime.run(
        stream_id=stream_id,
        session_id=session_id or ctx.session_id,
        question="Read taxes",
        model="test/model",
        history=[],
        collection_ids=None,
    )
    return await runtime_tests._drain(backplane, stream_id)


@pytest.mark.parametrize("discovery", ["search_documents", "list_documents"])
async def test_discovered_document_handle_reads_and_survives_later_turn(
    ctx: runtime_tests._Ctx, discovery: str
) -> None:
    async with ctx.sessionmaker() as session:
        document = await session.get(models.Document, ctx.document_id)
        assert document is not None
        document.status = "ready"
        await session.commit()
    gateway = _DocumentGateway(discovery)
    events = await _document_turn(ctx, gateway)
    assert events[-1]["type"] == "done"
    assert "Document: taxes.pdf" in gateway.replies[-1]
    assert "$14,600" in gateway.replies[-1]
    async with ctx.sessionmaker() as session:
        stored = await HandleRepository(session, ctx.principal, ctx.session_id).load()
    assert stored["D1"] == {"kind": "document", "document_id": str(ctx.document_id)}
    later = _DocumentGateway()
    await _document_turn(ctx, later)
    assert "$14,600" in later.replies[-1]


async def test_persisted_document_handle_rechecks_access_and_conversation(
    ctx: runtime_tests._Ctx,
) -> None:
    async with ctx.sessionmaker() as session:
        await HandleRepository(session, ctx.principal, ctx.session_id).append(
            {"D1": {"kind": "document", "document_id": str(ctx.document_id)}}
        )
        other = await ChatSessionRepository(session, ctx.tenant_id).create(
            owner_id=ctx.principal.user_id, model="test/model"
        )
        document = await session.get(models.Document, ctx.document_id)
        assert document is not None
        document.status = "ready"
        await session.commit()
    readable = _DocumentGateway()
    assert (await _document_turn(ctx, readable))[-1]["type"] == "done"
    assert "$14,600" in readable.replies[-1]
    async with ctx.sessionmaker() as session:
        document = await session.get(models.Document, ctx.document_id)
        assert document is not None
        # Readiness is unchanged: only access to the previously read identity is revoked.
        document.owner_id = uuid.uuid4()
        await session.commit()
    revoked = _DocumentGateway()
    await _document_turn(ctx, revoked)
    assert revoked.replies[-1] == "Document not found."
    foreign_conversation = _DocumentGateway()
    await _document_turn(ctx, foreign_conversation, other.id)
    assert foreign_conversation.replies[-1] == "Invalid document id."
    assert all("$14,600" not in reply for reply in revoked.replies + foreign_conversation.replies)


_LITERAL_MARKDOWN = (
    "The literal syntax is `[S1]`. Unknown code: `[S99]` and `[D2]`.\n\n"
    "```text\n[S1] [S99] [W99]\n```\n\n"
    '[label [S99]](https://example.test/[W99] "[D2]")\n\n'
    "<div>\n[S99]\n</div>\n\n"
    "> **Supported [S2].** Literal `[S99]`; unknown [S99].\n"
)


def test_marker_cleanup_preserves_ineligible_markdown_and_only_removes_text_markers() -> None:
    cleaned, selected = select_cited_handles(_LITERAL_MARKDOWN, {"S1", "S2"})
    assert cleaned == _LITERAL_MARKDOWN.replace("unknown [S99].", "unknown .")
    assert selected == ["S2"]
    assert select_cited_handles(cleaned, {"S2"}) == (cleaned, ["S2"])


@pytest.mark.parametrize(
    "literal",
    [
        "`[S99]`",
        "![image [S99]](https://example.test/[S99])",
        '[[S99]](https://example.test/[S99] "[S99]")',
        "[S99](https://example.test/[S99])",
        "[S99][ref]\n\n[ref]: https://example.test/[S99]",
        '<span title="[S99]">literal</span>',
        "\\[S99]",
        "    [S99]\n",
        "~~~text\n[S99]\n~~~",
    ],
)
def test_marker_cleanup_keeps_literal_occurrences_with_identical_citation_text(
    literal: str,
) -> None:
    answer = f"{literal}\r\n\r\n- **Claim [S1]**; unknown [S99].\r\n  `[S99]`\r\n"
    cleaned, selected = select_cited_handles(answer, {"S1"})
    assert cleaned == answer.replace("unknown [S99]", "unknown ")
    assert selected == ["S1"]


async def test_runtime_preserves_literal_markers_through_both_resolution_passes(
    ctx: runtime_tests._Ctx,
) -> None:
    answer = "The literal syntax is `[S1]`.\n\n```text\n[S99]\n```"
    events, _ = await _run_answer(
        ctx,
        answer=answer,
        passages=[runtime_tests._passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")],
    )
    assert _visible_answer(events) == answer
    assert events[-1]["data"]["citationCount"] == 0
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
    assert (
        next(message.content for message in messages if message.role.value == "assistant") == answer
    )
