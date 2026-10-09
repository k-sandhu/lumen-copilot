"""PR570 R4: document dependencies follow the complete prompt, transitively."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from app.api.v1.chat import _to_chat_messages
from app.core.config import get_settings
from app.db import models
from app.db.repositories import CitationRepository, MessageRepository, SessionSummaryRepository
from app.domain.entities import MessageRole
from app.domain.llm import ChatMessage, StreamEvent, ToolCall
from app.domain.llm import Role as LlmRole
from app.domain.retrieval import DocumentMatch, DocumentText
from app.domain.tools import RiskTier, ToolHandlerResult
from app.llm.context import assemble_context
from app.realtime.backplane import InMemoryBackplane
from app.services.assistant_runtime import AssistantRunConfig
from app.services.chat_runtime import ChatRuntime
from app.services.chat_service import ChatService
from app.services.tools.impls.recall import _read_conversation
from app.services.tools.types import ToolContext, ToolDefinition
from app.services.transcript_recall import SessionTranscriptReader
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context
from tests.test_chat_runtime import _Ctx, _drain, _FakeRetrieval, _passage, _ScriptedGateway
from tests.test_chat_runtime import ctx as provenance_ctx  # noqa: F401


@pytest.fixture
def ctx(provenance_ctx: _Ctx) -> _Ctx:  # noqa: F811
    return provenance_ctx


class DocumentRetrieval(_FakeRetrieval):
    permitted = True

    def __init__(self, document_id: uuid.UUID, chunk_id: uuid.UUID) -> None:
        passage = _passage(document_id, chunk_id, "secret-q3-plan.pdf")
        from dataclasses import replace

        super().__init__([replace(passage, text="The secret margin was 41%.")])
        self.document_id = document_id

    async def get_document(self, **kwargs: Any) -> DocumentText:
        return DocumentText(
            document_id=self.document_id,
            document_name="secret-q3-plan.pdf",
            text="The secret margin was 41%.",
        )

    async def list_documents(self, **kwargs: Any) -> list[DocumentMatch]:
        return [
            DocumentMatch(document_id=self.document_id, document_name="secret-q3-plan.pdf", score=1)
        ]

    async def permitted_document_names(self, **kwargs: Any) -> dict[uuid.UUID, str]:
        return (
            {self.document_id: "secret-q3-plan.pdf"}
            if self.permitted and self.document_id in kwargs["document_ids"]
            else {}
        )


async def run_context_answer(
    *,
    factory: Any,
    principal: Any,
    session_id: uuid.UUID,
    retrieval_factory: Any,
    call: ToolCall | None = None,
    ending: str = "answer",
    history: list[ChatMessage] | None = None,
    needle: str = "41%",
    summary: str | None = None,
    extra_tools: tuple[ToolDefinition, ...] = (),
) -> uuid.UUID:
    """Await provider receipt of real context, then round-trip the persisted turn."""
    answer = f"Was the secret margin {needle}?" if ending == "question" else f"Confirmed: {needle}."
    final = (
        [
            StreamEvent(
                tool_calls=(
                    ToolCall(
                        id="ask",
                        name="ask_user",
                        arguments={
                            "question": answer,
                            "options": [{"label": "Yes"}, {"label": "No"}],
                        },
                    ),
                ),
                finish_reason="tool_calls",
            )
        ]
        if ending == "question"
        else [StreamEvent(text=answer), StreamEvent(finish_reason="stop")]
    )

    class ReceiptGateway(_ScriptedGateway):
        async def stream_tools(self, messages: Any, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            if call is None or self.calls:
                assert any(
                    needle in m.content
                    and m.role in (LlmRole.ASSISTANT, LlmRole.TOOL, LlmRole.USER)
                    for m in messages
                ), "the actual document-derived context must reach the provider"
            async for event in super().stream_tools(messages, **kwargs):
                yield event

    gateway = ReceiptGateway(
        ([[StreamEvent(tool_calls=(call,), finish_reason="tool_calls")]] if call else []) + [final],
        synthesis=final,
    )
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex

    async def mcp_factory(_session: Any) -> dict[str, ToolDefinition]:
        return {tool.name: tool for tool in extra_tools}

    runtime = ChatRuntime(
        sessionmaker=factory,
        gateway=gateway,
        backplane=backplane,
        principal=principal,
        request_id="pr570-r4",
        source_ip="unknown",
        suggestions_enabled=False,
        default_max_tool_turns=1 if ending == "forced" else 3,
        retrieval_factory=retrieval_factory,
        mcp_tools_factory=mcp_factory,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=session_id,
        question="Confirm the earlier point",
        model="anthropic/claude-opus-4.8",
        history=history or [],
        collection_ids=None,
        summary=summary,
        assistant_config=(
            AssistantRunConfig(
                system_prompt="You are grounded.",
                allowed=frozenset({"ask_user", *(tool.name for tool in extra_tools)}),
                collection_ids=None,
                model=None,
            )
            if extra_tools
            else None
        ),
    )
    envelopes = await _drain(backplane, stream_id)
    assert envelopes[-1]["type"] == "done", envelopes[-1]
    async with factory() as session:
        messages = await MessageRepository(session, principal.tenant_id).list_for_session(
            session_id
        )
        copied_id = uuid.UUID(envelopes[-1]["data"]["messageId"])
        copied = next(m for m in messages if m.id == copied_id)
        assert copied.content == answer
        assert (copied.question is not None) == (ending == "question")
        if ending == "question" or call is None or call.name != "search_text":
            assert (
                await CitationRepository(session, principal.tenant_id).list_for_message(copied.id)
                == []
            )
        # Fixed ordering, including SQLite's second-resolution timestamp ties;
        # no sleeps or dependence on how quickly adjacent answers execute.
        prior_stamps = [
            m.created_at.replace(tzinfo=UTC)
            for m in messages
            if m.role is MessageRole.ASSISTANT and m.id != copied.id
        ]
        stamp = max(prior_stamps, default=datetime(2021, 1, 1, tzinfo=UTC)) + timedelta(minutes=1)
        await session.execute(
            update(models.Message).where(models.Message.id == copied.id).values(created_at=stamp)
        )
        await session.commit()
        return copied.id


async def compact_and_recall(
    factory: Any, principal: Any, session_id: uuid.UUID, last: uuid.UUID, retrieval: Any
) -> Any:
    async with factory() as session:
        message = await MessageRepository(session, principal.tenant_id).get(last)
        assert message is not None
        await SessionSummaryRepository(session, principal.tenant_id).upsert_summary(
            session_id,
            summary="Earlier discussion",
            covers_through_message_id=last,
            covered_created_at=message.created_at,
        )
        await session.commit()
    async with factory() as session:
        return await _read_conversation(
            {},
            ToolContext(
                principal=principal,
                retrieval=retrieval,
                transcript=SessionTranscriptReader(
                    session=session,
                    principal=principal,
                    session_id=session_id,
                    compaction_cursor=(message.created_at, last),
                ),
            ),
        )


@pytest.mark.parametrize(
    "tool,ending",
    [
        ("get_document", "answer"),
        ("get_document", "question"),
        ("get_document", "forced"),
        ("search_text", "question"),
        ("list_documents", "answer"),
    ],
)
async def test_non_recall_document_context_is_withheld_after_revocation(
    ctx: _Ctx, tool: str, ending: str
) -> None:
    """R3-001: real document tools taint answers AND zero-citation questions."""
    retrieval = DocumentRetrieval(ctx.document_id, ctx.chunk_id)
    needle = "secret-q3-plan.pdf" if tool == "list_documents" else "41%"
    copied = await run_context_answer(
        factory=ctx.sessionmaker,
        principal=ctx.principal,
        session_id=ctx.session_id,
        retrieval_factory=lambda _session: retrieval,
        call=ToolCall(
            id="read", name=tool, arguments={"document_id": str(ctx.document_id), "query": "margin"}
        ),
        ending=ending,
        needle=needle,
    )
    granted = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, copied, retrieval
    )
    assert needle in granted.content
    retrieval.permitted = False
    revoked = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, copied, retrieval
    )
    assert needle not in revoked.content, "document-derived prose must not become source-free"
    assert "withheld_turns" not in revoked.payload
    async with ctx.sessionmaker() as session:
        assert (
            await MessageRepository(session, ctx.tenant_id).source_documents_for_messages([copied])
        )[copied] == (ctx.document_id,)


async def live_history(factory: Any, principal: Any, session_id: uuid.UUID) -> list[ChatMessage]:
    """Use the actual service → repository → router projection, not hand-built metadata."""
    async with factory() as session:
        sent = await ChatService(
            session,
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            settings=get_settings(),
            denials=denial_context(
                RecordingDurableAuditTransactions(), session, principal.tenant_id, principal.user_id
            ),
        ).send_message(
            session_id, content="Confirm the figure", model=None, backplane=InMemoryBackplane()
        )
        assert sent is not None
        await session.commit()
        return _to_chat_messages(sent.history)


async def test_live_history_preserves_multi_hop_document_dependencies(ctx: _Ctx) -> None:
    """R3-002: A cites doc → B copies A → C copies B → recall after revocation."""
    retrieval = DocumentRetrieval(ctx.document_id, ctx.chunk_id)
    ids = [
        await run_context_answer(
            factory=ctx.sessionmaker,
            principal=ctx.principal,
            session_id=ctx.session_id,
            retrieval_factory=lambda _session: retrieval,
            call=ToolCall(id="search", name="search_text", arguments={"query": "margin"}),
        )
    ]
    for _ in range(2):
        ids.append(
            await run_context_answer(
                factory=ctx.sessionmaker,
                principal=ctx.principal,
                session_id=ctx.session_id,
                retrieval_factory=lambda _session: retrieval,
                history=await live_history(ctx.sessionmaker, ctx.principal, ctx.session_id),
            )
        )
    granted = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, ids[-1], retrieval
    )
    assert "41%" in granted.content
    retrieval.permitted = False
    revoked = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, ids[-1], retrieval
    )
    assert "41%" not in revoked.content, "live-history copying must preserve transitive provenance"
    assert "withheld_turns" not in revoked.payload
    async with ctx.sessionmaker() as session:
        sources = await MessageRepository(session, ctx.tenant_id).source_documents_for_messages(ids)
        assert all(sources[mid] == (ctx.document_id,) for mid in ids)


async def test_recall_then_live_history_copy_preserves_dependencies(ctx: _Ctx) -> None:
    """Exact R3-002: a recall-derived answer survives a further live-history copy."""
    from tests.test_chat_runtime import _run_recall_only_answer

    retrieval = DocumentRetrieval(ctx.document_id, ctx.chunk_id)
    original, recalled, _ = await _run_recall_only_answer(
        sessionmaker=ctx.sessionmaker,
        principal=ctx.principal,
        session_id=ctx.session_id,
        document_id=ctx.document_id,
        chunk_id=ctx.chunk_id,
        retrieval_factory=lambda _session: retrieval,
        ending="answer",
        source="citation",
        compact_copy=False,
    )
    copied = await run_context_answer(
        factory=ctx.sessionmaker,
        principal=ctx.principal,
        session_id=ctx.session_id,
        retrieval_factory=lambda _session: retrieval,
        history=await live_history(ctx.sessionmaker, ctx.principal, ctx.session_id),
    )
    retrieval.permitted = False
    revoked = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, copied, retrieval
    )
    assert "41%" not in revoked.content
    assert "withheld_turns" not in revoked.payload
    async with ctx.sessionmaker() as session:
        ids = [original, recalled, copied]
        sources = await MessageRepository(session, ctx.tenant_id).source_documents_for_messages(ids)
        assert all(sources[mid] == (ctx.document_id,) for mid in ids)


@pytest.mark.parametrize("unknown", ["history", "tool", "error", "summary"])
async def test_unknown_prompt_provenance_is_never_known_empty(ctx: _Ctx, unknown: str) -> None:
    """Legacy rows, unclassified tool content/errors and summary text fail closed."""
    history: list[ChatMessage] = []
    summary = None
    call = None
    extra_tools: tuple[ToolDefinition, ...] = ()
    if unknown == "history":
        async with ctx.sessionmaker() as session:
            await MessageRepository(session, ctx.tenant_id).add(
                session_id=ctx.session_id, role=MessageRole.ASSISTANT, content="legacy secret 41%"
            )
            await session.commit()
        history = await live_history(ctx.sessionmaker, ctx.principal, ctx.session_id)
    elif unknown == "summary":
        summary = "Earlier secret margin 41%"
    else:

        async def unclassified(args: Any, context: Any) -> ToolHandlerResult:
            return ToolHandlerResult(
                content="secret 41%",
                ok=unknown != "error",
                error="tool_error" if unknown == "error" else None,
            )

        extra_tools = (
            ToolDefinition(
                name="mcp:r4:unclassified",
                description="unknown provenance",
                json_schema={"type": "object"},
                handler=unclassified,
                risk_tier=RiskTier.T0,
                read_only=True,
            ),
        )
        call = ToolCall(id="unknown", name="mcp:r4:unclassified", arguments={})
    copied = await run_context_answer(
        factory=ctx.sessionmaker,
        principal=ctx.principal,
        session_id=ctx.session_id,
        retrieval_factory=lambda _session: _FakeRetrieval([]),
        call=call,
        history=history,
        summary=summary,
        extra_tools=extra_tools,
    )
    async with ctx.sessionmaker() as session:
        assert (
            await MessageRepository(session, ctx.tenant_id).source_documents_for_messages([copied])
        )[copied] is None
    recalled = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, copied, _FakeRetrieval([])
    )
    assert "41%" not in recalled.content


async def test_provably_empty_context_remains_recallable(ctx: _Ctx) -> None:
    copied = await run_context_answer(
        factory=ctx.sessionmaker,
        principal=ctx.principal,
        session_id=ctx.session_id,
        retrieval_factory=lambda _session: _FakeRetrieval([]),
        history=[
            ChatMessage(
                role=LlmRole.USER, content="My chosen number is 41%", source_document_ids=()
            )
        ],
    )
    async with ctx.sessionmaker() as session:
        assert (
            await MessageRepository(session, ctx.tenant_id).source_documents_for_messages([copied])
        )[copied] == ()
    recalled = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, copied, _FakeRetrieval([])
    )
    assert "41%" in recalled.content


async def test_complete_tool_result_unions_all_provenance_fields(ctx: _Ctx) -> None:
    """A generic tool has independent passage, document-name and transitive sources."""
    named_document, transitive_document = uuid.uuid4(), uuid.uuid4()
    all_sources = {ctx.document_id, named_document, transitive_document}
    passage = _passage(ctx.document_id, ctx.chunk_id, "secret-q3-plan.pdf")

    async def complete_result(args: Any, context: Any) -> ToolHandlerResult:
        return ToolHandlerResult(
            content="The secret margin was 41%.",
            passages=(passage,),
            document_ids=(named_document,),
            source_document_ids=(transitive_document,),
        )

    class PermittedSources(_FakeRetrieval):
        permitted = set(all_sources)

        async def permitted_document_names(self, **kwargs: Any) -> dict[uuid.UUID, str]:
            return {d: "source.pdf" for d in kwargs["document_ids"] if d in self.permitted}

    retrieval = PermittedSources([])
    tool = ToolDefinition(
        name="mcp:r4:complete",
        description="Complete source metadata",
        json_schema={"type": "object"},
        handler=complete_result,
        risk_tier=RiskTier.T0,
        read_only=True,
    )
    copied = await run_context_answer(
        factory=ctx.sessionmaker,
        principal=ctx.principal,
        session_id=ctx.session_id,
        retrieval_factory=lambda _session: retrieval,
        extra_tools=(tool,),
        call=ToolCall(id="complete", name=tool.name, arguments={}),
        ending="question",
    )
    async with ctx.sessionmaker() as session:
        sources = await MessageRepository(session, ctx.tenant_id).source_documents_for_messages(
            [copied]
        )
        assert set(sources[copied]) == all_sources
    granted = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, copied, retrieval
    )
    assert "41%" in granted.content
    retrieval.permitted.remove(ctx.document_id)
    revoked = await compact_and_recall(
        ctx.sessionmaker, ctx.principal, ctx.session_id, copied, retrieval
    )
    assert "41%" not in revoked.content
    assert "withheld_turns" not in revoked.payload


def test_evidence_context_without_source_metadata_is_unknown() -> None:
    assembled = assemble_context(
        model="test",
        system_prompt="system",
        history=[],
        question="question",
        evidence_lines=["secret-q3-plan.pdf document reference"],
        counter=len,
        max_input_resolver=lambda _model: 100_000,
    )
    assert assembled.messages[1].source_document_ids is None
