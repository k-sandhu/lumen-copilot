"""read_document takes one handle instead of a mutually exclusive pair (#771).

Notebook 08 (section 9) measured the ``document``/``around`` pair failing 21-38%
of identifier calls for three models, and every read for gpt-6-luna, whatever
the error message said. One parameter that takes either handle measured 0%.
"""

# ruff: noqa: F811
from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.llm import Role as LlmRole
from app.domain.llm import StreamEvent, ToolCall
from app.domain.tools import ERROR_BAD_ARGS, ERROR_NOT_FOUND
from app.realtime.backplane import InMemoryBackplane
from app.services.tools.registry import get_tool
from app.services.tools.types import ToolContext
from tests.test_chat_runtime import _Ctx, _passage, _runtime
from tests.test_chat_runtime import _FakeRetrieval as _ChatRetrieval
from tests.test_chat_runtime import ctx as ctx
from tests.test_task_tools import _CHUNK, _DOC, _SECRET, _context, _FakeRetrieval, _handles
from tests.test_tool_schema_contract import _schema_runner, _World
from tests.test_tool_schema_contract import world as world

_ALLOWED = frozenset({"search_passages", "find_documents", "read_document"})
_UNAVAILABLE = (
    "No document or passage is available for that handle in this conversation and "
    "scope. handle takes a D handle (for example D2) or an S handle (for example S3) "
    "shown by search_passages or find_documents. If a passage may have changed, "
    "search again."
)


def _ctx(retrieval: _FakeRetrieval, *, second_conversation: bool = False) -> ToolContext:
    return replace(
        _context(retrieval, handles=_handles(second_conversation=second_conversation)),
        allowed_tools=_ALLOWED,
    )


def _reads(retrieval: _FakeRetrieval) -> list[dict[str, Any]]:
    return [kwargs for name, kwargs in retrieval.calls if name == "read_document"]


def test_schema_has_one_required_handle_and_no_exclusive_pair() -> None:
    schema = get_tool("read_document").json_schema

    assert set(schema["properties"]) == {
        "handle",
        "start",
        "end",
        "max_passages",
        "collection_ids",
        "document_ids",
    }
    assert schema["required"] == ["handle"]
    description = schema["properties"]["handle"]["description"]
    assert "D handle" in description and "S handle" in description


async def test_d_handle_opens_the_document_range() -> None:
    retrieval = _FakeRetrieval()

    result = await get_tool("read_document").handler(
        {"handle": "D1", "start": 5, "end": 30, "max_passages": 3}, _ctx(retrieval)
    )

    assert result.ok and _SECRET in result.content
    [read] = _reads(retrieval)
    assert (read["document_id"], read["start"], read["end"], read["max_passages"]) == (
        _DOC,
        5,
        30,
        3,
    )


async def test_document_uuid_still_opens_the_document() -> None:
    retrieval = _FakeRetrieval()

    result = await get_tool("read_document").handler({"handle": str(_DOC)}, _ctx(retrieval))

    assert result.ok
    [read] = _reads(retrieval)
    assert (read["document_id"], read["start"], read["end"]) == (_DOC, 0, None)


async def test_s_handle_opens_around_the_passage_and_ignores_a_filled_range() -> None:
    retrieval = _FakeRetrieval()

    # Models that fill every optional field send a range with the S handle.
    result = await get_tool("read_document").handler(
        {"handle": "S1", "start": 0, "end": 10}, _ctx(retrieval)
    )

    assert result.ok and _SECRET in result.content
    assert retrieval.calls[0] == (
        "read_passages",
        {
            "principal": retrieval.calls[0][1]["principal"],
            "chunk_ids": [_CHUNK],
            "collection_ids": None,
            "document_ids": None,
        },
    )
    [read] = _reads(retrieval)
    assert (read["document_id"], read["start"], read["end"]) == (_DOC, 0, len(_SECRET) + 1200)


@pytest.mark.parametrize(
    ("handle", "readable", "second_conversation", "changed"),
    [
        pytest.param("D999", True, False, False, id="forged-document-handle"),
        pytest.param("S999", True, False, False, id="forged-passage-handle"),
        pytest.param("D1", True, True, False, id="other-conversation-document"),
        pytest.param("S1", True, True, False, id="other-conversation-passage"),
        pytest.param("D1", False, False, False, id="revoked-document"),
        pytest.param("S1", False, False, False, id="revoked-passage"),
        pytest.param("S1", True, False, True, id="changed-passage"),
        pytest.param(str(uuid.uuid4()), False, False, False, id="unknown-uuid"),
        pytest.param("Quarterly Plan.pdf", True, False, False, id="filename"),
        pytest.param("Quarterly Plan", True, False, False, id="filename-stem"),
        pytest.param("W1", True, False, False, id="web-handle"),
        pytest.param("", True, False, False, id="empty"),
    ],
)
async def test_unavailable_handles_share_one_corrective_non_disclosing_reply(
    handle: str, readable: bool, second_conversation: bool, changed: bool
) -> None:
    retrieval = _FakeRetrieval(
        readable=readable, passage_text=f"{_SECRET} changed" if changed else _SECRET
    )

    result = await get_tool("read_document").handler(
        {"handle": handle}, _ctx(retrieval, second_conversation=second_conversation)
    )

    assert not result.ok
    assert result.error == ERROR_NOT_FOUND
    assert result.summary == "document unavailable"
    assert result.content == _UNAVAILABLE
    assert _SECRET not in result.content
    assert "Quarterly Plan" not in result.content


async def test_corrective_reply_names_only_offered_tools() -> None:
    context = replace(_ctx(_FakeRetrieval()), allowed_tools=frozenset({"read_document"}))

    result = await get_tool("read_document").handler({"handle": "plan.pdf"}, context)

    assert "search_passages" not in result.content
    assert "find_documents" not in result.content
    assert "(for example S3) shown earlier in this conversation." in result.content


def _runner_context(world: _World, retrieval: _FakeRetrieval) -> ToolContext:
    return ToolContext(
        principal=world.principal,
        retrieval=retrieval,  # type: ignore[arg-type]
        handles=_handles(),
        allowed_tools=_ALLOWED,
    )


async def test_runner_rejects_the_measured_document_plus_around_shape_before_io(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _FakeRetrieval()
    runner = _schema_runner(world, get_tool("read_document"), monkeypatch)

    result = await runner.run(
        call=ToolCall(
            id="mutex",
            name="read_document",
            arguments={"document": "D1", "around": "S1", "max_passages": 2},
        ),
        context=_runner_context(world, retrieval),
    )

    assert result.error == ERROR_BAD_ARGS
    assert "Invalid arguments.around: remove unknown properties" in result.content
    assert retrieval.calls == []


@pytest.mark.parametrize(
    ("arguments", "first_call"),
    [
        pytest.param(
            {
                "handle": "S1",
                "start": None,
                "end": None,
                "max_passages": None,
                "collection_ids": None,
                "document_ids": None,
            },
            "read_passages",
            id="wire-nulls-passage",
        ),
        pytest.param({"handle": "S1", "start": 0, "end": 3000}, "read_passages", id="filled-range"),
        pytest.param({"handle": "D1", "start": 0, "end": 3000}, "read_document", id="document"),
    ],
)
async def test_runner_replays_handle_reads(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    first_call: str,
) -> None:
    retrieval = _FakeRetrieval()
    runner = _schema_runner(world, get_tool("read_document"), monkeypatch)

    result = await runner.run(
        call=ToolCall(id="read", name="read_document", arguments=arguments),
        context=_runner_context(world, retrieval),
    )

    assert result.ok, result.content
    assert retrieval.calls[0][0] == first_call
    assert _reads(retrieval)[0]["document_id"] == _DOC


class _ReadingRetrieval(_ChatRetrieval):
    """The chat-runtime fake plus a range read over its one permitted passage."""

    async def read_document(self, **kwargs: Any) -> SimpleNamespace:
        passage = self._passages[0]
        return SimpleNamespace(
            document_id=passage.document_id,
            passages=[passage],
            total_length=passage.char_end,
            returned_start=0,
            returned_end=passage.char_end,
            next_start=None,
        )


class _SearchThenReadGateway:
    """Search, read the D handle the header showed, read around the S handle, answer."""

    def __init__(self) -> None:
        self.tool_messages: list[str] = []

    async def stream_tools(self, messages: Any, **kwargs: object) -> AsyncIterator[StreamEvent]:
        del kwargs
        self.tool_messages = [m.content for m in messages if m.role is LlmRole.TOOL]
        step = len(self.tool_messages)
        if step == 0:
            call = ToolCall(id="search", name="search_passages", arguments={"query": "deduction"})
        elif step == 1:
            match = re.search(r"\(document (D[1-9][0-9]*), chars", self.tool_messages[0])
            assert match, self.tool_messages[0]
            call = ToolCall(id="read-d", name="read_document", arguments={"handle": match[1]})
        elif step == 2:
            call = ToolCall(id="read-s", name="read_document", arguments={"handle": "S1"})
        else:
            yield StreamEvent(text="The deduction is $14,600 [S1].")
            yield StreamEvent(finish_reason="stop")
            return
        yield StreamEvent(tool_calls=(call,), finish_reason="tool_calls")


async def test_search_header_handles_open_reads_through_the_chat_runtime(ctx: _Ctx) -> None:
    gateway = _SearchThenReadGateway()
    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = _runtime(
        ctx,
        gateway=gateway,
        retrieval=_ReadingRetrieval([_passage(ctx.document_id, ctx.chunk_id, "taxes.pdf")]),
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

    search, by_document, by_passage = gateway.tool_messages
    assert search.startswith("[S1] taxes.pdf (document D2, chars 100-143)")
    for read in (by_document, by_passage):
        assert "[S1] taxes.pdf\nThe standard deduction for 2024 is $14,600." in read
        assert "Available range: 0-143; returned 0-143." in read
    events = [env async for env in backplane.subscribe(stream_id)]
    assert events[-1]["type"] == "done"
    assert events[-1]["data"]["citationCount"] == 1
