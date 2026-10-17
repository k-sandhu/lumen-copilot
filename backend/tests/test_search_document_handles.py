"""search_passages shows each hit's D handle, the token read_document accepts (#772).

Notebook 08 (section 9) measured every filename read failing until the search
output showed a D handle. These tests pin the header in both the handler and
the provider transcript and the compaction snippet's byte identity. The
corrective reply for an unavailable read lives with ``read_document``'s
``handle`` parameter (#771, tests/test_read_document_handle.py).
"""

# ruff: noqa: F811
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from app.domain.llm import Role as LlmRole
from app.domain.llm import StreamEvent, ToolCall
from app.realtime.backplane import InMemoryBackplane
from app.services.tools.handles import EvidenceHandles
from app.services.tools.registry import get_tool
from tests.test_chat_runtime import _Ctx, _FakeRetrieval, _passage, _runtime
from tests.test_chat_runtime import ctx as ctx
from tests.test_task_tools import _DOC, _SECRET, _context
from tests.test_task_tools import _FakeRetrieval as _ToolRetrieval


async def test_search_passages_header_names_document_handle_and_range() -> None:
    retrieval = _ToolRetrieval()
    other = replace(
        retrieval.passage,
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="Other.pdf",
        char_start=40,
        char_end=90,
    )
    same = replace(retrieval.passage, chunk_id=uuid.uuid4(), char_start=500, char_end=520)
    passages = [retrieval.passage, other, same]

    async def search_text(**kwargs: Any) -> list[Any]:
        return passages

    retrieval.search_text = search_text  # type: ignore[method-assign]
    handles = EvidenceHandles()

    result = await get_tool("search_passages").handler(
        {"query": "plan"}, _context(retrieval, handles=handles)
    )

    assert result.ok
    blocks = result.content.split("\n\n")
    assert blocks[0] == (
        f"[S1] Quarterly Plan.pdf (document D2, chars 0-{len(_SECRET)})\n{_SECRET}"
    )
    assert blocks[1].startswith("[S3] Other.pdf (document D4, chars 40-90)\n")
    # A second hit in the same document reuses its D handle.
    assert blocks[2].startswith("[S5] Quarterly Plan.pdf (document D2, chars 500-520)\n")
    assert handles.document_id("D2") == _DOC
    assert handles.document_id("D4") == other.document_id


class _SearchGateway:
    """Search once, record the provider tool transcript, then cite the hit."""

    def __init__(self) -> None:
        self.calls = 0
        self.tool_messages: list[str] = []

    async def stream_tools(self, messages: Any, **kwargs: object) -> AsyncIterator[StreamEvent]:
        del kwargs
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                tool_calls=(
                    ToolCall(id="search-1", name="search_passages", arguments={"query": "fact"}),
                ),
                finish_reason="tool_calls",
            )
            return
        self.tool_messages = [m.content for m in messages if m.role is LlmRole.TOOL]
        yield StreamEvent(text="The deduction is $14,600 [S1].")
        yield StreamEvent(finish_reason="stop")


async def test_provider_transcript_shows_document_handle_and_compaction_keeps_it(
    ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.evidence_handles import HandleRepository
    from app.db.repositories import CitationRepository, MessageRepository
    from app.services import chat_runtime

    captured: list[dict[str, tuple[str, ...]]] = []
    original_fit = chat_runtime.fit_transcript

    def recording_fit(*args: Any, **kwargs: Any) -> Any:
        captured.append(dict(kwargs.get("cited_snippets") or {}))
        return original_fit(*args, **kwargs)

    monkeypatch.setattr(chat_runtime, "fit_transcript", recording_fit)
    gateway = _SearchGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_FakeRetrieval([_passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")]),
        backplane=backplane,
    )

    await runtime.run(
        stream_id=stream_id,
        session_id=ctx.session_id,
        question="What is the deduction?",
        model="anthropic/claude-opus-4.8",
        history=[],
        collection_ids=None,
    )

    [content] = gateway.tool_messages
    assert content == (
        "[S1] taxes.pdf (document D2, chars 100-143)\n"
        "The standard deduction for 2024 is $14,600."
    )
    # Compaction re-embeds cited evidence verbatim, so its snippet must be the
    # exact block the model saw, header included.
    snippets = [s for fit in captured for s in fit.get("search-1", ())]
    assert snippets and all(snippet == content for snippet in snippets)
    async with ctx.sessionmaker() as session:
        registry = await HandleRepository(session, ctx.principal, ctx.session_id).load()
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(ctx.session_id)
        answer = next(m for m in messages if m.role.value == "assistant")
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message(answer.id)
    assert registry["D2"] == {"kind": "document", "document_id": str(ctx.document_id)}
    # The D handle navigates; only the inline S handle becomes a citation.
    assert [citation.chunk_id for citation in citations] == [ctx.chunk_id]
