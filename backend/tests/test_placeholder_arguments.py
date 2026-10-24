"""Placeholder optional arguments never act as filters (#773).

Some models fill every optional field with ``""``, ``[]`` or ``"null"``. For a
read-only tool the runner treats those placeholders like provider ``null``:
the field is omitted, so its documented omission default applies. Required
fields and side-effecting tools keep the exact values the model sent.
"""

# ruff: noqa: F811
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.llm import ToolCall
from app.domain.retrieval import RetrievedPassage
from app.domain.tool_schema import legacy_arguments
from app.domain.tools import RiskTier, ToolHandlerResult
from app.services.tools.handles import EvidenceHandles
from app.services.tools.registry import get_tool
from app.services.tools.types import (
    ApprovalDecision,
    ApprovalRequest,
    ToolContext,
    ToolDefinition,
)
from tests.test_tool_schema_contract import _schema_runner, _World
from tests.test_tool_schema_contract import world as world

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "ids": {"type": "array", "items": {"type": "string"}},
        "cursor": {"type": "string"},
        "mode": {"type": "string", "enum": ["", "full"]},
        "nested": {
            "type": "object",
            "properties": {"note": {"type": "string"}, "tags": {"type": "array"}},
        },
        "required_list": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["query", "required_list"],
}


@pytest.mark.parametrize(
    "placeholder", ["", "null", []], ids=["empty-string", "null-string", "empty-array"]
)
def test_read_only_placeholders_in_optional_fields_are_omitted(placeholder: object) -> None:
    arguments = {
        "query": "x",
        "required_list": [],
        "ids": placeholder,
        "cursor": placeholder,
        "nested": {"note": placeholder, "tags": placeholder},
    }

    normalized = legacy_arguments(arguments, _SCHEMA, omit_placeholders=True)

    assert normalized == {"query": "x", "required_list": [], "nested": {}}


def test_required_fields_and_real_values_keep_their_exact_value() -> None:
    arguments = {"query": "", "required_list": [], "ids": ["a"], "cursor": "c1"}

    assert legacy_arguments(arguments, _SCHEMA, omit_placeholders=True) == arguments


def test_placeholder_listed_by_the_schema_is_a_real_value() -> None:
    arguments = {"query": "x", "required_list": ["r"], "mode": ""}

    assert legacy_arguments(arguments, _SCHEMA, omit_placeholders=True) == arguments


def test_placeholders_are_kept_unless_requested() -> None:
    arguments = {"query": "x", "required_list": ["r"], "ids": [], "cursor": "", "note": None}
    schema = {**_SCHEMA, "properties": {**_SCHEMA["properties"], "note": {"type": "string"}}}

    assert legacy_arguments(arguments, schema) == {
        "query": "x",
        "required_list": ["r"],
        "ids": [],
        "cursor": "",
    }


class _CorpusRetrieval:
    """Records forwarded filters and returns one permitted passage or document."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.document_id = uuid.uuid4()
        self.passage = RetrievedPassage(
            chunk_id=uuid.uuid4(),
            document_id=self.document_id,
            document_name="permit.pdf",
            ord=0,
            text="Permits expire after two years.",
            char_start=0,
            char_end=31,
            score=0.9,
        )

    async def search_text(self, **kwargs: Any) -> list[RetrievedPassage]:
        self.calls.append(("search_text", kwargs))
        return [self.passage]

    async def find_documents(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("find_documents", kwargs))
        item = SimpleNamespace(
            document_id=self.document_id,
            title="Permit",
            filename="permit.pdf",
            source="upload",
            mime_type="application/pdf",
            source_path=None,
        )
        return SimpleNamespace(items=[item], next_cursor=None)


def _context(
    world: _World, retrieval: _CorpusRetrieval, *, document_ids: list[uuid.UUID] | None = None
) -> ToolContext:
    return ToolContext(
        principal=world.principal,
        retrieval=retrieval,  # type: ignore[arg-type]
        handles=EvidenceHandles(),
        document_ids=document_ids,
    )


async def test_runner_replays_glm_search_with_empty_filters_as_unfiltered(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _CorpusRetrieval()
    runner = _schema_runner(world, get_tool("search_passages"), monkeypatch)
    arguments = {
        "query": "permit expiry",
        "k": 6,
        "collection_ids": [],
        "document_ids": [],
        "source": "",
        "mime_type": "null",
        "created_after": "",
        "modified_before": "null",
    }

    result = await runner.run(
        call=ToolCall(id="glm-search", name="search_passages", arguments=arguments),
        context=_context(world, retrieval),
    )

    assert result.ok, result.content
    assert result.hit_count == 1
    assert retrieval.calls == [
        (
            "search_text",
            {
                "principal": world.principal,
                "query": "permit expiry",
                "k": 6,
                "collection_ids": None,
                "document_ids": None,
            },
        )
    ]


async def test_runner_replays_glm_discovery_placeholders_as_omitted(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _CorpusRetrieval()
    runner = _schema_runner(world, get_tool("find_documents"), monkeypatch)
    arguments = {
        "query": "permit",
        "limit": 10,
        "cursor": "",
        "sort": "",
        "collection_ids": [],
        "document_ids": [],
        "source": "null",
        "mime_type": "",
        "created_after": "null",
        "created_before": "",
        "modified_after": "",
        "modified_before": "null",
    }

    result = await runner.run(
        call=ToolCall(id="glm-find", name="find_documents", arguments=arguments),
        context=_context(world, retrieval),
    )

    assert result.ok, result.content
    assert result.hit_count == 1
    [(name, forwarded)] = retrieval.calls
    assert name == "find_documents"
    assert forwarded["cursor"] is None
    assert forwarded["sort"] == "title_asc"
    for field in (
        "collection_ids",
        "document_ids",
        "source",
        "mime_type",
        "created_after",
        "created_before",
        "modified_after",
        "modified_before",
    ):
        assert forwarded[field] is None, field


async def test_empty_filter_placeholder_keeps_the_pinned_document_scope(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _CorpusRetrieval()
    pinned = [retrieval.document_id]
    runner = _schema_runner(world, get_tool("search_passages"), monkeypatch)

    result = await runner.run(
        call=ToolCall(
            id="pinned",
            name="search_passages",
            arguments={"query": "permit", "document_ids": [], "collection_ids": []},
        ),
        context=_context(world, retrieval, document_ids=pinned),
    )

    assert result.ok
    assert retrieval.calls[0][1]["document_ids"] == pinned
    assert retrieval.calls[0][1]["collection_ids"] is None


async def test_legitimately_empty_effective_scope_still_returns_nothing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _CorpusRetrieval()
    runner = _schema_runner(world, get_tool("search_passages"), monkeypatch)

    # A pin to a revoked document leaves an empty effective scope: placeholder
    # normalisation must not widen it to the whole corpus (INV-2).
    result = await runner.run(
        call=ToolCall(
            id="revoked-pin",
            name="search_passages",
            arguments={"query": "permit", "document_ids": []},
        ),
        context=_context(world, retrieval, document_ids=[]),
    )

    assert result.ok
    assert result.hit_count == 0
    assert retrieval.calls == []
    assert "permit.pdf" not in result.content


async def test_side_effecting_tool_receives_its_exact_empty_values(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        return ToolHandlerResult(content="must not run")

    definition = ToolDefinition(
        name="write_probe",
        description="Write with an optional clearable field",
        json_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "description": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["path"],
        },
        handler=handler,
        risk_tier=RiskTier.T1,
        read_only=False,
        requires_approval=True,
    )
    runner = _schema_runner(world, definition, monkeypatch)
    runner._session_id = uuid.uuid4()
    requests: list[ApprovalRequest] = []

    class DenyingGate:
        async def request(self, request: ApprovalRequest) -> ApprovalDecision:
            requests.append(request)
            return ApprovalDecision.deny("test_denied", "Denied for test.")

    runner._gate = DenyingGate()  # type: ignore[assignment]
    arguments = {"path": "notes.md", "description": "", "labels": []}

    await runner.run(
        call=ToolCall(id="write", name=definition.name, arguments=arguments),
        context=ToolContext(principal=world.principal, retrieval=_CorpusRetrieval()),  # type: ignore[arg-type]
    )

    assert [request.arguments for request in requests] == [arguments]
