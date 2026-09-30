"""Compatibility and security tests for task-shaped corpus tools (#627)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.auth.principal import Principal
from app.domain.entities import Role
from app.domain.retrieval import RetrievedPassage
from app.domain.tools import RiskTier
from app.services.tools.compatibility import permitted_names
from app.services.tools.handles import EvidenceHandles
from app.services.tools.registry import default_allowlist, get_tool, registered_names
from app.services.tools.types import ToolContext

_DOC = uuid.UUID("00000000-0000-0000-0000-000000000101")
_CHUNK = uuid.UUID("00000000-0000-0000-0000-000000000102")
_SECRET = "restricted document body must never escape"


class _FakeRetrieval:
    def __init__(self, *, readable: bool = True, passage_text: str = _SECRET) -> None:
        self.readable = readable
        self.passage_text = passage_text
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.passage = RetrievedPassage(
            chunk_id=_CHUNK,
            document_id=_DOC,
            document_name="Quarterly Plan.pdf",
            ord=0,
            text=passage_text,
            char_start=0,
            char_end=len(_SECRET),
            score=0.9,
        )

    async def search_text(self, **kwargs: Any) -> list[RetrievedPassage]:
        self.calls.append(("search_text", kwargs))
        return [self.passage]

    async def read_passages(self, **kwargs: Any) -> list[RetrievedPassage]:
        self.calls.append(("read_passages", kwargs))
        if (
            not self.readable
            or kwargs.get("collection_ids") == []
            or kwargs.get("document_ids") == []
        ):
            return []
        return [self.passage]

    async def find_documents(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("find_documents", kwargs))
        if not self.readable:
            return SimpleNamespace(items=[], next_cursor=None)
        item = SimpleNamespace(
            document_id=_DOC,
            title="Quarterly Plan",
            filename="plan.pdf",
            source_path="Finance/plan.pdf",
            source="gdrive",
            mime_type="application/pdf",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            metadata={"team": "Finance"},
        )
        return SimpleNamespace(items=[item], next_cursor=None)

    async def read_document(self, **kwargs: Any) -> SimpleNamespace | None:
        self.calls.append(("read_document", kwargs))
        if (
            not self.readable
            or kwargs.get("collection_ids") == []
            or kwargs.get("document_ids") == []
        ):
            return None
        return SimpleNamespace(
            document_id=_DOC,
            document_name="Quarterly Plan.pdf",
            passages=[self.passage],
            total_length=len(_SECRET),
            returned_start=0,
            returned_end=len(_SECRET),
            next_start=None,
        )

    async def get_document(self, **kwargs: Any) -> SimpleNamespace | None:
        self.calls.append(("get_document", kwargs))
        if not self.readable:
            return None
        return SimpleNamespace(document_id=_DOC, document_name="Quarterly Plan.pdf", text=_SECRET)


def _principal() -> Principal:
    return Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.MEMBER,))


def _handles(*, second_conversation: bool = False) -> EvidenceHandles:
    entries = {
        "D1": {"kind": "document", "document_id": str(_DOC)},
        "S1": {
            "kind": "passage",
            "document_id": str(_DOC),
            "chunk_id": str(_CHUNK),
            "char_start": 0,
            "char_end": len(_SECRET),
            "fingerprint": hashlib.sha256(_SECRET.encode()).hexdigest(),
        },
    }
    if second_conversation:
        entries = {}
    return EvidenceHandles(existing=entries)


def _context(
    retrieval: _FakeRetrieval,
    *,
    handles: EvidenceHandles | None = None,
    collection_ids: list[uuid.UUID] | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> ToolContext:
    return ToolContext(
        principal=_principal(),
        retrieval=retrieval,  # type: ignore[arg-type]
        collection_ids=collection_ids,
        document_ids=document_ids,
        handles=handles,
    )


def test_task_tool_aliases_are_registered_as_flat_read_only_t0_tools() -> None:
    expected = {"search_passages", "find_documents", "read_document"}
    assert expected <= registered_names()
    for name in expected:
        tool = get_tool(name)
        assert tool.risk_tier is RiskTier.T0
        assert tool.read_only is True
        assert tool.requires_approval is False
        assert tool.json_schema["type"] == "object"
        assert tool.json_schema.get("additionalProperties") is False

    assert {"query", "limit", "cursor", "sort", "source", "mime_type"} <= set(
        get_tool("find_documents").json_schema["properties"]
    )
    assert {"document", "around", "start", "end", "max_passages"} <= set(
        get_tool("read_document").json_schema["properties"]
    )


def test_default_allowlist_prefers_canonical_corpus_names_and_keeps_ask_user() -> None:
    canonical = {"search_passages", "find_documents", "read_document"}
    legacy = {"search_text", "search_documents", "list_documents", "get_document"}

    assert canonical <= default_allowlist()
    assert "ask_user" in default_allowlist()
    assert legacy.isdisjoint(default_allowlist())
    for name in legacy:
        assert name in registered_names()
        assert get_tool(name).default_offered is False


def test_disabling_a_legacy_tool_blocks_its_whole_compatibility_family() -> None:
    allowed = frozenset(
        {
            "search_passages",
            "search_text",
            "find_documents",
            "search_documents",
            "list_documents",
            "read_document",
            "get_document",
        }
    )

    permitted = permitted_names(allowed, {"search_text"})

    assert "search_passages" not in permitted
    assert "search_text" not in permitted
    assert {
        "find_documents",
        "search_documents",
        "list_documents",
        "read_document",
        "get_document",
    } <= permitted


def test_explicit_legacy_allowlist_never_expands_to_canonical_name() -> None:
    permitted = permitted_names(frozenset({"search_text"}), set())

    assert permitted == frozenset({"search_text"})
    assert "search_passages" not in permitted


@pytest.mark.asyncio
async def test_find_documents_forwards_flat_filters_and_mints_document_handles() -> None:
    retrieval = _FakeRetrieval()
    handles = _handles(second_conversation=True)
    result = await get_tool("find_documents").handler(
        {
            "query": "Quarterly Plan",
            "limit": 7,
            "sort": "created_desc",
            "source": "gdrive",
            "mime_type": "application/pdf",
            "created_after": "2026-07-01T00:00:00Z",
            "created_before": "2026-09-01T00:00:00Z",
            "modified_after": "2026-08-01T00:00:00Z",
            "modified_before": "2026-09-01T00:00:00Z",
            "collection_ids": ["00000000-0000-0000-0000-000000000201"],
            "document_ids": [str(_DOC)],
        },
        _context(retrieval, handles=handles),
    )

    assert result.ok
    assert retrieval.calls[0][0] == "find_documents"
    forwarded = retrieval.calls[0][1]
    assert forwarded["query"] == "Quarterly Plan"
    assert forwarded["limit"] == 7
    assert forwarded["sort"] == "created_desc"
    assert forwarded["source"] == "gdrive"
    assert forwarded["mime_type"] == "application/pdf"
    assert forwarded["modified_after"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert forwarded["modified_before"] == datetime(2026, 9, 1, tzinfo=UTC)
    assert forwarded["collection_ids"] == [uuid.UUID("00000000-0000-0000-0000-000000000201")]
    assert forwarded["document_ids"] == [_DOC]
    assert "D1" in str(result)
    assert handles.resolve("D1") == {"kind": "document", "document_id": str(_DOC)}


@pytest.mark.asyncio
async def test_search_passages_returns_complete_evidence_and_passage_handle() -> None:
    retrieval = _FakeRetrieval()
    handles = _handles(second_conversation=True)
    result = await get_tool("search_passages").handler(
        {"query": "the plan"}, _context(retrieval, handles=handles)
    )

    assert result.ok
    assert _SECRET in result.content
    assert handles.resolve("S1") is not None
    assert result.hit_count == 1


@pytest.mark.asyncio
async def test_read_document_resolves_document_and_passage_handles() -> None:
    retrieval = _FakeRetrieval()
    handles = _handles()

    by_document = await get_tool("read_document").handler(
        {"document": "D1", "start": 0, "max_passages": 3},
        _context(retrieval, handles=handles),
    )
    by_passage = await get_tool("read_document").handler(
        {"around": "S1", "start": 0, "max_passages": 3},
        _context(retrieval, handles=handles),
    )

    assert by_document.ok and by_passage.ok
    document_calls = [kwargs for name, kwargs in retrieval.calls if name == "read_document"]
    assert [call["document_id"] for call in document_calls] == [_DOC, _DOC]
    assert _SECRET in by_document.content and _SECRET in by_passage.content
    assert retrieval.calls[1][0] == "read_passages"
    assert retrieval.calls[1][1]["chunk_ids"] == [_CHUNK]
    assert retrieval.calls[2][0] == "read_document"


@pytest.mark.asyncio
async def test_legacy_get_document_accepts_document_handle_and_forged_handle_does_no_io() -> None:
    retrieval = _FakeRetrieval()
    ctx = _context(retrieval, handles=_handles())

    valid = await get_tool("get_document").handler({"document_id": "D1"}, ctx)
    forged = await get_tool("get_document").handler({"document_id": "D999"}, ctx)

    assert valid.ok and _SECRET in valid.content
    assert retrieval.calls == [("get_document", {"principal": ctx.principal, "document_id": _DOC})]
    assert not forged.ok
    assert _SECRET not in forged.content
    assert "Quarterly Plan" not in forged.content


@pytest.mark.asyncio
async def test_changed_passage_handle_fingerprint_is_not_readable() -> None:
    retrieval = _FakeRetrieval(passage_text=f"{_SECRET} changed")
    handles = _handles()

    result = await get_tool("read_document").handler(
        {"around": "S1"}, _context(retrieval, handles=handles)
    )

    assert not result.ok
    assert retrieval.calls[0][0] == "read_passages"
    assert all(call[0] != "read_document" for call in retrieval.calls)
    assert _SECRET not in result.content
    assert "Quarterly Plan" not in result.content


@pytest.mark.asyncio
async def test_empty_scope_intersection_returns_no_document_content() -> None:
    retrieval = _FakeRetrieval()
    allowed_collection = uuid.UUID("00000000-0000-0000-0000-000000000201")
    disallowed_collection = uuid.UUID("00000000-0000-0000-0000-000000000202")
    result = await get_tool("read_document").handler(
        {"document": "D1", "collection_ids": [str(disallowed_collection)]},
        _context(retrieval, handles=_handles(), collection_ids=[allowed_collection]),
    )

    assert not result.ok
    assert retrieval.calls[0][1]["collection_ids"] == []
    assert _SECRET not in result.content
    assert "Quarterly Plan" not in result.content


@pytest.mark.asyncio
async def test_forged_or_other_conversation_handle_reveals_nothing() -> None:
    retrieval = _FakeRetrieval()
    ctx = _context(retrieval, handles=_handles(second_conversation=True))

    forged = await get_tool("read_document").handler({"document": "D999"}, ctx)
    foreign = await get_tool("read_document").handler({"around": "S1"}, ctx)

    assert not forged.ok and not foreign.ok
    assert retrieval.calls == []
    assert _SECRET not in forged.content + foreign.content
    assert "Quarterly Plan" not in forged.content + foreign.content
    assert "Finance/plan.pdf" not in forged.content + foreign.content


@pytest.mark.asyncio
async def test_revoked_document_permission_returns_no_metadata_or_body() -> None:
    retrieval = _FakeRetrieval(readable=False)
    result = await get_tool("read_document").handler(
        {"document": "D1"}, _context(retrieval, handles=_handles())
    )

    assert result.hit_count in (None, 0)
    assert _SECRET not in result.content
    assert "Quarterly Plan" not in result.content
    assert "Finance/plan.pdf" not in result.content
