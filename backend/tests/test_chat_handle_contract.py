"""Grounded answer tests for inline evidence handles and refusal semantics."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from app.domain.llm import StreamEvent, ToolCall
from tests.test_chat_runtime import ctx as _runtime_ctx

pytest_plugins = ("tests.test_chat_runtime",)
ctx = _runtime_ctx


def _visible_answer(envs: list[dict[str, object]]) -> str:
    """Replay answer deltas using the wire's whole-turn retraction rule."""
    answer = ""
    for env in envs:
        if env["type"] == "delta":
            data = env["data"]
            assert isinstance(data, dict)
            answer += str(data["text"])
        elif env["type"] == "event" and env.get("name") == "answer_retract":
            answer = ""
    return answer


class _HandleGateway:
    """Offer one permitted retrieval, then answer with the configured text."""

    def __init__(self, answer: str) -> None:
        self._answer = answer
        self._calls = 0

    async def stream_tools(self, messages: object, **kwargs: object) -> AsyncIterator[StreamEvent]:
        del messages, kwargs
        if self._calls == 0:
            self._calls += 1
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="search-1", name="search_text", arguments={"query": "fact"}),
                ),
                finish_reason="tool_calls",
            )
            return
        self._calls += 1
        yield StreamEvent(text=self._answer)
        yield StreamEvent(finish_reason="stop")


async def _run_answer(
    ctx: object, *, answer: str, passages: list[object]
) -> tuple[list[dict[str, object]], object]:
    from app.realtime.backplane import InMemoryBackplane
    from tests.test_chat_runtime import _Ctx, _drain, _FakeRetrieval, _runtime

    assert isinstance(ctx, _Ctx)
    retrieval = _FakeRetrieval(passages)  # type: ignore[arg-type]
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=_HandleGateway(answer),
        retrieval=retrieval,
        backplane=backplane,
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What was the fact?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    return await _drain(backplane, stream_id), ctx


async def _run_web_answer(
    ctx: object,
    *,
    answer: str,
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, object]], object, _WebHandleGateway]:
    from app.domain.tools import ToolResult
    from app.realtime.backplane import InMemoryBackplane
    from tests.test_chat_runtime import _Ctx, _drain, _FakeRetrieval, _runtime

    assert isinstance(ctx, _Ctx)
    body = "Policy says keep receipts. " * 80
    payload = {
        "sourceType": "web",
        "results": [
            {
                "title": "Expense policy",
                "url": url,
                "snippet": "Short snippet fallback.",
                "fetchedPassage": body,
            }
        ],
    }
    gateway = _WebHandleGateway(answer)
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([]),
        backplane=backplane,
    )

    async def fake_tool_batch(**kwargs: object) -> list[ToolResult]:
        calls = kwargs["calls"]
        assert isinstance(calls, list | tuple) and len(calls) == 1
        call = calls[0]
        return [
            ToolResult(
                call_id=call.id,
                name=call.name,
                ok=True,
                content="Web search completed.",
                payload=payload,
            )
        ]

    monkeypatch.setattr(runtime, "_run_tool_batch", fake_tool_batch)
    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What is the policy?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )
    return await _drain(backplane, stream_id), ctx, gateway


class _WebHandleGateway:
    """Ask for one web result, record the tool transcript, then answer."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0
        self.tool_messages: list[str] = []

    async def stream_tools(self, messages: object, **kwargs: object) -> AsyncIterator[StreamEvent]:
        del kwargs
        if self.calls == 0:
            self.calls += 1
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="web-call", name="search_text", arguments={"query": "policy"}),
                ),
                finish_reason="tool_calls",
            )
            return
        self.calls += 1
        self.tool_messages = [
            message.content
            for message in messages  # type: ignore[union-attr]
            if getattr(message, "role", None).value == "tool"
        ]
        yield StreamEvent(text=self.answer)
        yield StreamEvent(finish_reason="stop")


async def test_refusal_after_retrieval_persists_and_reports_zero_citations(ctx: object) -> None:
    from app.db.repositories import CitationRepository, MessageRepository
    from app.services.prompts.grounded_answer import NO_SOURCES_FALLBACK
    from tests.test_chat_runtime import _Ctx, _passage

    assert isinstance(ctx, _Ctx)
    envs, _ = await _run_answer(
        ctx,
        answer=NO_SOURCES_FALLBACK,
        passages=[_passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")],
    )

    terminal = next(env for env in envs if env["type"] == "done")
    assert terminal["data"]["citationCount"] == 0  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = next(message for message in messages if message.role.value == "assistant")
        assert assistant.content == NO_SOURCES_FALLBACK
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message(assistant.id)
    assert citations == []


async def test_inline_handle_persists_only_the_cited_retrieved_passage(ctx: object) -> None:
    from app.db.repositories import ChunkRepository, CitationRepository, MessageRepository
    from app.domain.retrieval import RetrievedPassage
    from tests.test_chat_runtime import _Ctx, _passage

    assert isinstance(ctx, _Ctx)
    async with ctx.sessionmaker() as session:
        second_chunk = await ChunkRepository(session, ctx.tenant_id).add(
            document_id=ctx.document_id,
            ord=1,
            text="A separate retrieved fact.",
            char_start=200,
            char_end=226,
        )
        await session.commit()
    first = _passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")
    second = RetrievedPassage(
        chunk_id=second_chunk.id,
        document_id=ctx.document_id,
        document_name="taxes.pdf",
        ord=1,
        text="A separate retrieved fact.",
        char_start=200,
        char_end=226,
        score=0.8,
    )

    envs, _ = await _run_answer(
        ctx,
        answer="The deduction is $14,600 [S1].",
        passages=[first, second],
    )

    deltas = _visible_answer(envs)
    assert deltas == "The deduction is $14,600 [S1]."
    assert not any(env["type"] == "event" and env.get("name") == "answer_retract" for env in envs)
    terminal = next(env for env in envs if env["type"] == "done")
    assert terminal["data"]["citationCount"] == 1  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = next(message for message in messages if message.role.value == "assistant")
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message(assistant.id)
    assert assistant.content == "The deduction is $14,600 [S1]."
    assert len(citations) == 1
    assert citations[0].chunk_id == ctx.chunk_id


async def test_unknown_inline_handle_is_stripped_and_never_creates_a_citation(
    ctx: object,
) -> None:
    from app.db.repositories import CitationRepository, MessageRepository
    from tests.test_chat_runtime import _Ctx, _passage

    assert isinstance(ctx, _Ctx)
    envs, _ = await _run_answer(
        ctx,
        answer="This claim has no valid source [S999].",
        passages=[_passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")],
    )

    # The first model turn is speculative and may contain the unknown marker.
    # A retraction clears those deltas; only the corrected answer is visible.
    assert any(env["type"] == "event" and env.get("name") == "answer_retract" for env in envs)
    assert "[S999]" not in _visible_answer(envs)
    terminal = next(env for env in envs if env["type"] == "done")
    assert terminal["data"]["citationCount"] == 0  # type: ignore[index]
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        assistant = next(message for message in messages if message.role.value == "assistant")
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message(assistant.id)
    assert "[S999]" not in assistant.content
    assert citations == []


def test_web_citation_handle_survives_tool_result_compaction() -> None:
    from app.domain.llm import ChatMessage, Role, ToolCall
    from app.llm.context import ContextConfig, fit_transcript
    from app.services.chat_runtime import _cited_snippets_by_call

    excerpt = "[W1] Finance policy\nhttps://example.test/policy\nThe limit is $500."
    cited = _cited_snippets_by_call({}, {}, {"web-call": (excerpt,)})
    messages = [
        ChatMessage(role=Role.SYSTEM, content="system"),
        ChatMessage(role=Role.USER, content="question"),
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="web-call", name="web_search", arguments={}),),
        ),
        ChatMessage(
            role=Role.TOOL,
            content="Older web result. " + ("irrelevant " * 500) + excerpt,
            tool_call_id="web-call",
            name="web_search",
        ),
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="later", name="search_text", arguments={}),),
        ),
        ChatMessage(role=Role.TOOL, content="new result", tool_call_id="later", name="search_text"),
    ]

    fitted = fit_transcript(
        messages,
        model="fake/model",
        config=ContextConfig(
            fallback_max_input_tokens=100_000,
            output_headroom_tokens=0,
            compaction_digest_chars=30,
            proactive_compaction_enabled=True,
        ),
        counter=len,
        max_input_resolver=lambda _model: 100_000,
        cited_snippets=cited,
    )

    assert len(fitted[3].content) < len(messages[3].content)
    assert excerpt in fitted[3].content
    assert "[W1]" in fitted[3].content


async def test_web_handle_round_trips_only_the_cited_safe_excerpt(
    ctx: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings
    from app.db.repositories import CitationRepository
    from app.domain.entities import MessageRole
    from app.services.chat_service import ChatService
    from tests.test_chat_runtime import _Ctx

    assert isinstance(ctx, _Ctx)
    events, _, gateway = await _run_web_answer(
        ctx,
        answer="The expense limit is documented here [W1].",
        url="https://example.test/expense-policy",
        monkeypatch=monkeypatch,
    )
    visible = "Policy says keep receipts. " * 80
    excerpt = visible[:700]
    tool_transcript = "\n".join(gateway.tool_messages)
    assert "[W1] Expense policy" in tool_transcript
    assert excerpt in tool_transcript
    assert "[Truncated web excerpt.]" in tool_transcript
    assert _visible_answer(events) == "The expense limit is documented here [W1]."

    done = next(event for event in events if event["type"] == "done")
    assert done["data"]["citationCount"] == 1  # type: ignore[index]
    citation_event = next(
        event
        for event in events
        if event["type"] == "event" and event.get("name") == "web_citation"
    )
    data = citation_event["data"]
    assert isinstance(data, dict)
    assert data["handle"] == "W1"
    assert data["snippet"] == excerpt

    async with ctx.sessionmaker() as session:
        page = await ChatService(
            session,
            tenant_id=ctx.tenant_id,
            owner_id=ctx.principal.user_id,
            settings=get_settings(),
        ).list_messages(ctx.session_id, cursor=None, limit=None)
        assert page is not None
        assistant = next(item for item in page.items if item.message.role == MessageRole.ASSISTANT)
        web = assistant.web_citations
        corpus_citations = await CitationRepository(session, ctx.tenant_id).list_for_message(
            assistant.message.id
        )
    assert assistant.message.content == "The expense limit is documented here [W1]."
    assert corpus_citations == []
    assert len(web) == 1
    assert str(web[0].id) == data["id"]
    assert web[0].handle == "W1"
    assert web[0].snippet == excerpt
    assert not hasattr(web[0], "document_id")


async def test_uncited_and_unsafe_web_results_never_persist_a_web_citation(
    ctx: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.evidence_handles import WebCitationRepository
    from app.db.repositories import MessageRepository
    from app.domain.entities import MessageRole
    from app.services.prompts.grounded_answer import NO_SOURCES_FALLBACK
    from tests.test_chat_runtime import _Ctx

    assert isinstance(ctx, _Ctx)
    uncited_events, _, _ = await _run_web_answer(
        ctx,
        answer="There was no supporting result.",
        url="https://example.test/expense-policy",
        monkeypatch=monkeypatch,
    )
    uncited_terminal = next(event for event in uncited_events if event["type"] == "done")
    assert uncited_terminal["data"]["citationCount"] == 0  # type: ignore[index]
    assert not any(event.get("name") == "web_citation" for event in uncited_events)
    async with ctx.sessionmaker() as session:
        uncited_messages = await MessageRepository(session, ctx.tenant_id).list_for_session(
            ctx.session_id
        )
        uncited_assistant = next(
            message for message in uncited_messages if message.role == MessageRole.ASSISTANT
        )
        uncited_web = await WebCitationRepository(session, ctx.tenant_id).list_for_messages(
            [uncited_assistant.id]
        )
    assert uncited_web == {}

    # The same model handle cannot authorize a non-HTTP URL. With no valid web
    # evidence selected, a marker-only answer reduces to the honest fallback.
    unsafe_events, _, _ = await _run_web_answer(
        ctx,
        answer="[W1]",
        url="file:///etc/passwd",
        monkeypatch=monkeypatch,
    )
    unsafe_terminal = next(event for event in unsafe_events if event["type"] == "done")
    assert unsafe_terminal["data"]["citationCount"] == 0  # type: ignore[index]
    assert not any(event.get("name") == "web_citation" for event in unsafe_events)
    assert _visible_answer(unsafe_events) == NO_SOURCES_FALLBACK
