"""document_ids filters accept the D handles the tools show (#774).

No canonical corpus tool shows a document UUID, so a UUID-only filter could
only be met by a fabricated id that silently matched nothing. Filters now
resolve D handles through the conversation registry and check every value
against current permissions; anything else gets one field-specific error.
"""

# ruff: noqa: F811
from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest

from app.domain.llm import ToolCall
from app.domain.tools import ERROR_BAD_ARGS
from app.services.tools.registry import get_tool
from app.services.tools.types import ToolContext
from tests.test_document_discovery import discovery_session as discovery_session
from tests.test_task_tools import _DOC, _SECRET, _handles
from tests.test_task_tools import _context as _base_context
from tests.test_task_tools import _FakeRetrieval as _TaskRetrieval
from tests.test_tool_schema_contract import _schema_runner, _World
from tests.test_tool_schema_contract import world as world

_ALLOWED = frozenset({"search_passages", "find_documents", "read_document"})


class _FakeRetrieval(_TaskRetrieval):
    """Adds the permission check filters use; ``permitted`` is the readable set."""

    def __init__(self, *, permitted: set[uuid.UUID] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.permitted = {_DOC} if permitted is None else permitted

    async def permitted_document_names(
        self, *, principal: object, document_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        self.calls.append(("permitted_document_names", {"document_ids": document_ids}))
        return {d: "Quarterly Plan.pdf" for d in document_ids if d in self.permitted}


def _context(retrieval: _FakeRetrieval, **kwargs: Any) -> ToolContext:
    kwargs.setdefault("handles", _handles())
    return replace(_base_context(retrieval, **kwargs), allowed_tools=_ALLOWED)


def _read_arguments(value: str) -> dict[str, Any]:
    """read_document's identifier field: ``document`` today, ``handle`` once #771 lands."""
    properties = get_tool("read_document").json_schema["properties"]
    return {"handle" if "handle" in properties else "document": value}


def _forwarded(retrieval: _FakeRetrieval, name: str) -> dict[str, Any]:
    return next(kwargs for call, kwargs in retrieval.calls if call == name)


@pytest.mark.parametrize(
    ("tool", "arguments", "retrieval_call"),
    [
        ("search_passages", {"query": "plan"}, "search_text"),
        ("find_documents", {}, "find_documents"),
        ("read_document", None, "read_document"),
    ],
)
async def test_document_ids_accepts_conversation_d_handles(
    tool: str, arguments: dict[str, Any] | None, retrieval_call: str
) -> None:
    retrieval = _FakeRetrieval()
    arguments = _read_arguments("D1") if arguments is None else arguments

    result = await get_tool(tool).handler(
        {**arguments, "document_ids": ["D1"]}, _context(retrieval)
    )

    assert result.ok, result.content
    assert _forwarded(retrieval, retrieval_call)["document_ids"] == [_DOC]


async def test_permitted_uuids_and_handles_for_the_same_document_are_deduplicated() -> None:
    retrieval = _FakeRetrieval()

    result = await get_tool("search_passages").handler(
        {"query": "plan", "document_ids": [str(_DOC), "D1"]}, _context(retrieval)
    )

    assert result.ok
    assert _forwarded(retrieval, "search_text")["document_ids"] == [_DOC]


_UNAVAILABLE = (
    "document_ids has a value that names no available document: {value}. document_ids "
    "takes D handles shown in this conversation (for example D2), which find_documents "
    "returns, or omit it to search every permitted document."
)


@pytest.mark.parametrize(
    ("value", "permitted", "second_conversation"),
    [
        pytest.param("D999", None, False, id="forged-handle"),
        pytest.param("D1", None, True, id="other-conversation-handle"),
        pytest.param(str(uuid.uuid4()), None, False, id="unknown-uuid"),
        pytest.param("D1", set(), False, id="revoked-document"),
        pytest.param(str(_DOC), set(), False, id="forbidden-uuid"),
        pytest.param("Quarterly Plan.pdf", None, False, id="filename"),
        pytest.param("S1", None, False, id="passage-handle"),
        pytest.param("D1D1D1D1-0000-0000-0000-000000000000", None, False, id="forged-uuid"),
    ],
)
@pytest.mark.parametrize("tool", ["search_passages", "find_documents", "read_document"])
async def test_unavailable_document_filter_gets_one_non_disclosing_error(
    tool: str, value: str, permitted: set[uuid.UUID] | None, second_conversation: bool
) -> None:
    retrieval = _FakeRetrieval(permitted=permitted)
    arguments: dict[str, Any] = {"document_ids": [value]}
    if tool == "search_passages":
        arguments["query"] = "plan"
    if tool == "read_document":
        arguments.update(_read_arguments(str(_DOC)))
    context = _context(retrieval, handles=_handles(second_conversation=second_conversation))

    result = await get_tool(tool).handler(arguments, context)

    assert not result.ok
    assert result.error == ERROR_BAD_ARGS
    assert result.content == _UNAVAILABLE.format(value=repr(value))
    assert _SECRET not in result.content
    if value != "Quarterly Plan.pdf":
        assert "Quarterly Plan" not in result.content
    # No search, discovery or read runs over an unresolved filter.
    assert {call for call, _ in retrieval.calls} <= {"permitted_document_names"}


async def test_error_lists_each_unavailable_value_once_and_bounds_the_echo() -> None:
    retrieval = _FakeRetrieval()
    long_name = "x" * 200

    result = await get_tool("search_passages").handler(
        {"query": "plan", "document_ids": ["D1", "D9", long_name, "D9", "S1", "a.pdf"]},
        _context(retrieval),
    )

    assert not result.ok
    assert result.content.startswith(
        "document_ids has values that name no available document: 'D9', "
        f"'{'x' * 40}…', 'S1' and 1 more. "
    )
    assert long_name not in result.content


async def test_corrective_text_names_only_offered_discovery_tools() -> None:
    retrieval = _FakeRetrieval()
    context = replace(_context(retrieval), allowed_tools=frozenset({"search_passages"}))

    result = await get_tool("search_passages").handler(
        {"query": "plan", "document_ids": ["plan.pdf"]}, context
    )

    assert "find_documents" not in result.content
    assert "D handles shown in this conversation (for example D2), or omit it" in result.content


async def test_filter_outside_the_pinned_scope_says_the_filter_matched_nothing() -> None:
    retrieval = _FakeRetrieval()
    pinned = [uuid.UUID("00000000-0000-0000-0000-000000000999")]

    result = await get_tool("search_passages").handler(
        {"query": "plan", "document_ids": ["D1"]}, _context(retrieval, document_ids=pinned)
    )

    assert result.ok and result.hit_count == 0
    assert all(call != "search_text" for call, _ in retrieval.calls)
    assert result.content == (
        "No permitted documents in this conversation's scope match these filters. "
        "Remove or change a filter."
    )


async def test_filtered_search_with_no_hits_points_at_the_filter_not_only_the_query() -> None:
    retrieval = _FakeRetrieval()

    async def no_hits(**kwargs: Any) -> list[Any]:
        retrieval.calls.append(("search_text", kwargs))
        return []

    retrieval.search_text = no_hits  # type: ignore[method-assign]

    result = await get_tool("search_passages").handler(
        {"query": "plan", "document_ids": ["D1"]}, _context(retrieval)
    )

    assert result.ok and result.hit_count == 0
    assert result.content == (
        "No matching permitted passages in the filtered documents. Try a broader query "
        "or remove a filter."
    )


def test_document_ids_schema_advertises_d_handles() -> None:
    for name in ("search_passages", "find_documents", "read_document"):
        field = get_tool(name).json_schema["properties"]["document_ids"]
        assert field["items"] == {"type": "string"}
        assert "D handles" in field["description"]


async def test_runner_replays_a_d_handle_filter_end_to_end(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _FakeRetrieval()
    runner = _schema_runner(world, get_tool("search_passages"), monkeypatch)

    result = await runner.run(
        call=ToolCall(
            id="d-filter",
            name="search_passages",
            arguments={"query": "quarterly plan", "document_ids": ["D1"]},
        ),
        context=ToolContext(
            principal=world.principal,
            retrieval=retrieval,  # type: ignore[arg-type]
            handles=_handles(),
        ),
    )

    assert result.ok, result.content
    assert _forwarded(retrieval, "search_text")["document_ids"] == [_DOC]


async def test_real_permission_check_admits_granted_and_rejects_denied_revoked_and_foreign(
    discovery_session: tuple[Any, Any],
) -> None:
    from app.services.tools.handles import EvidenceHandles
    from tests.test_document_discovery import _principal, _service

    session, world = discovery_session
    handles = EvidenceHandles()
    shared, denied, revoked, foreign = (
        handles.document(world.ids[key]) for key in ("shared", "denied", "revoked", "foreign")
    )
    context = ToolContext(
        principal=_principal(world.alice, world.tenant),
        retrieval=_service(session),
        handles=handles,
        allowed_tools=_ALLOWED,
    )
    find = get_tool("find_documents")

    granted = await find.handler({"document_ids": [shared]}, context)
    rejected = [
        await find.handler({"document_ids": [h]}, context) for h in (denied, revoked, foreign)
    ]

    assert granted.ok and granted.document_ids == (world.ids["shared"],)
    assert all(not reply.ok and reply.error == ERROR_BAD_ARGS for reply in rejected)
    neutral = {
        reply.content.replace(repr(handle), "<value>")
        for reply, handle in zip(rejected, (denied, revoked, foreign), strict=True)
    }
    assert neutral == {_UNAVAILABLE.format(value="<value>")}
