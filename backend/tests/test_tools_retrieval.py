"""Retrieval tools behind the governed registry — dispatch + INV-2 filter (CC-7 #207).

The three retrieval tools (``search_text`` / ``search_documents`` / ``get_document``)
migrated onto the tool registry (issue #207 AC-1): these offline (in-memory SQLite)
tests assert the handlers still map a model's args onto the right permission-filtered
``retrieval/`` method and render the reply — and the headline INV-2 regression, that
a tool call as user A never returns user B's data (same tenant) nor another tenant's.
The semantic ``search_text`` path needs pgvector (live only), so the permission
assertions here use the relational tools, which run on SQLite and exercise the same
allow-set chokepoint. Registry discovery + governance metadata are asserted too, so
"adding a tool is a new file in impls/" (AC-1) has a mechanism.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.config import CANONICAL_EMBEDDING_DIMENSIONS
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.domain.audit import AuditActor
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import Embedding, ToolCall
from app.domain.retrieval import RetrievedPassage
from app.domain.tools import ERROR_BAD_ARGS, ERROR_NOT_PERMITTED, RiskTier
from app.retrieval import RetrievalService
from app.services.assistant_runtime import assemble_run_config
from app.services.assistants_service import AssistantsService
from app.services.audit import AuditSink
from app.services.tools.registry import default_allowlist, get_tool, registered_names, tool_specs
from app.services.tools.runner import ToolRunner
from app.services.tools.types import ToolContext
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context
from tests._db_helpers import copy_sqlite_schema

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_EMBED_DIM = CANONICAL_EMBEDDING_DIMENSIONS


class _FakeGateway:
    async def embed(
        self,
        inputs: list[str],
        *,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        return [Embedding(vector=[0.0] * _EMBED_DIM, model="fake") for _ in inputs]


def _principal(user_id: uuid.UUID, tenant_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(Role.MEMBER,))


class _World:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice: uuid.UUID,
        bob: uuid.UUID,
        carol: uuid.UUID,
        alice_doc: uuid.UUID,
        bob_doc: uuid.UUID,
        carol_doc: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice
        self.bob = bob
        self.carol = carol
        self.alice_doc = alice_doc
        self.bob_doc = bob_doc
        self.carol_doc = carol_doc


@pytest_asyncio.fixture
async def session_and_world() -> AsyncIterator[tuple[AsyncSession, _World]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            ta = await TenantRepository(session).create(name="Acme")
            tb = await TenantRepository(session).create(name="Globex")
            alice = await UserRepository(session, ta.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(session, ta.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            carol = await UserRepository(session, tb.id).create(
                email="carol@globex.test", password_hash="x", roles=[Role.MEMBER]
            )
            alice_doc = await _doc(session, ta.id, alice.id, "alice-taxes.txt", "Alice tax notes.")
            bob_doc = await _doc(session, ta.id, bob.id, "bob-secret.txt", "Bob private notes.")
            carol_doc = await _doc(session, tb.id, carol.id, "carol.txt", "Carol notes.")
            await session.commit()
            world = _World(
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice=alice.id,
                bob=bob.id,
                carol=carol.id,
                alice_doc=alice_doc,
                bob_doc=bob_doc,
                carol_doc=carol_doc,
            )
            yield session, world
    finally:
        await engine.dispose()


async def _doc(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    filename: str,
    text: str,
) -> uuid.UUID:
    coll = await CollectionRepository(session, tenant_id).create(owner_id=owner_id, name="c")
    doc = await DocumentRepository(session, tenant_id).create(
        owner_id=owner_id,
        collection_id=coll.id,
        filename=filename,
        mime_type="text/plain",
        size_bytes=len(text),
        storage_key=f"{tenant_id}/{filename}",
        acl_enforced=False,
        status=DocumentStatus.READY,
    )
    await ChunkRepository(session, tenant_id).replace_for_document(
        doc.id, [ChunkInput(text=text, char_start=0, char_end=len(text))]
    )
    return doc.id


def _ctx(session: AsyncSession, principal: Principal) -> ToolContext:
    service = RetrievalService(session, gateway=_FakeGateway())  # type: ignore[arg-type]
    return ToolContext(
        principal=principal,
        retrieval=service,
        collection_ids=None,
        default_k=6,
        allowed_tools=_RETRIEVAL_TOOLS | _OTHER_DEFAULT_TOOLS,
    )


async def _call(session: AsyncSession, principal: Principal, name: str, args: dict[str, object]):
    """Invoke a registered tool's handler directly (unit-level, no runner)."""
    handler = get_tool(name).handler
    return await handler(dict(args), _ctx(session, principal))


# --- Registry discovery + governance metadata (AC-1) ------------------------


_RETRIEVAL_TOOLS = frozenset({"search_text", "search_documents", "list_documents", "get_document"})
_CANONICAL_RETRIEVAL_TOOLS = frozenset({"search_passages", "find_documents", "read_document"})

#: The non-retrieval tools that are ALSO on the ad-hoc default allow-list. Named
#: exhaustively, deliberately: this set is a governance surface, so adding a
#: ``default_offered`` tool must be a visible, reviewed edit here rather than
#: something a subset assertion absorbs silently (deny-by-default, issue #219).
#: ``ask_user`` — the interactive clarifying-question tool (spec 0006 #429).
#: ``read_conversation`` — reading back THIS session's own compacted turns
#: (#569); not a widened reach, since the caller already owns and can read every
#: byte of that transcript through ``GET /chat/sessions/{id}/messages``.
_OTHER_DEFAULT_TOOLS = frozenset({"ask_user", "read_conversation"})


def test_registry_discovers_the_retrieval_tools() -> None:
    assert _RETRIEVAL_TOOLS <= registered_names()


def test_retrieval_tools_are_t0_read_only_no_approval() -> None:
    for name in _RETRIEVAL_TOOLS:
        defn = get_tool(name)
        assert defn.risk_tier is RiskTier.T0
        assert defn.read_only is True
        assert defn.requires_approval is False


def test_default_allowlist_is_the_read_only_retrieval_tools() -> None:
    # Ad-hoc chat's default allow-list = the read-only default-offered tools:
    # Canonical corpus tools plus main's non-retrieval defaults. Legacy tool
    # names remain callable, but are not default offered.
    assert default_allowlist() == _CANONICAL_RETRIEVAL_TOOLS | _OTHER_DEFAULT_TOOLS


def test_tool_specs_render_the_allowlist_to_llm_specs() -> None:
    specs = tool_specs(default_allowlist())
    names = {s.name for s in specs}
    assert names == _CANONICAL_RETRIEVAL_TOOLS | _OTHER_DEFAULT_TOOLS
    # Each spec carries the JSON-Schema parameters the model fills in.
    by_name = {s.name: s for s in specs}
    assert by_name["search_passages"].parameters["required"] == ["query"]
    # find_documents can enumerate with an empty query and needs no required args.
    assert by_name["find_documents"].parameters.get("required", []) == []


# --- search_documents -------------------------------------------------------


async def test_search_documents_returns_only_callers_docs(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "search_documents",
        {"name_or_query": "txt"},
    )
    # Alice sees only her own doc — never Bob's (same tenant) or Carol's (other).
    assert world.alice_doc in result.document_ids
    assert world.bob_doc not in result.document_ids
    assert world.carol_doc not in result.document_ids
    assert result.hit_count == 1


async def test_search_documents_blank_query_returns_nothing(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "search_documents",
        {"name_or_query": "  "},
    )
    assert result.hit_count == 0
    assert result.ok is True


# --- list_documents (enumeration, INV-1/INV-2) ------------------------------


async def test_list_documents_returns_only_callers_docs(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "list_documents",
        {},  # no args — enumeration needs no query
    )
    # Alice sees only her own doc — never Bob's (same tenant) or Carol's (other).
    assert world.alice_doc in result.document_ids
    assert world.bob_doc not in result.document_ids
    assert world.carol_doc not in result.document_ids
    assert result.hit_count == 1
    assert result.ok is True
    assert "alice-taxes.txt" in result.content


async def test_list_documents_empty_when_user_has_no_docs(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    # A principal in the tenant who owns nothing and was granted nothing.
    stranger = _principal(uuid.uuid4(), world.tenant_a)
    result = await _call(session, stranger, "list_documents", {})
    assert result.hit_count == 0
    assert result.ok is True  # "nothing here" is not an error
    assert "don't have access" in result.content.lower()


# --- get_document (INV-2 existence non-disclosure) --------------------------


async def test_get_document_returns_own_document(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": str(world.alice_doc)},
    )
    assert result.hit_count == 1
    assert "Alice tax notes" in result.content


async def test_get_document_other_owner_same_tenant_is_not_found(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    # Alice asks for Bob's document (same tenant) — must be "not found" (INV-2).
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": str(world.bob_doc)},
    )
    assert result.hit_count == 0
    assert "not found" in result.content.lower()


async def test_get_document_cross_tenant_is_not_found(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": str(world.carol_doc)},
    )
    assert result.hit_count == 0


async def test_get_document_invalid_id_is_a_bad_args_rejection(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": "not-a-uuid"},
    )
    # A malformed id is a tool-specific rejection the runner passes through, not a
    # crash: the handler returns ok=False with the bad-args code and a corrective
    # message the model can act on (#758), not the old dead-end line.
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert result.content != "Invalid document id."
    assert "document id:" in result.content
    assert "search_text" in result.content
    assert "search_documents" in result.content


async def test_get_document_long_text_returns_bounded_range_and_truncation_metadata(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    """A roomy document read returns the 2400-char context prefix with explicit bounds."""
    session, world = session_and_world
    text = "x" * 3000
    document_id = await _doc(session, world.tenant_a, world.alice, "long.txt", text)
    await session.commit()

    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": str(document_id)},
    )

    assert result.ok is True
    assert result.payload["returned_range"] == [0, 2400]
    assert result.payload["total_length"] == 3000
    assert result.payload["truncated"] is True
    assert "Returned range: 0-2400 of 3000 characters (end-exclusive)." in result.content
    assert "x" * 2400 in result.content
    assert "truncated" in result.content.lower()
    assert "search_text" in result.content


async def test_get_document_long_text_obeys_tight_context_budget(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    text = "y" * 3000
    document_id = await _doc(session, world.tenant_a, world.alice, "tight.txt", text)
    await session.commit()
    ctx = ToolContext(
        principal=_principal(world.alice, world.tenant_a),
        retrieval=RetrievalService(session, gateway=_FakeGateway()),  # type: ignore[arg-type]
        collection_ids=None,
        snippet_budget=300,
        allowed_tools=_RETRIEVAL_TOOLS | _OTHER_DEFAULT_TOOLS,
    )

    result = await get_tool("get_document").handler({"document_id": str(document_id)}, ctx)

    assert result.ok is True
    assert result.payload["returned_range"] == [0, 1200]
    assert result.payload["total_length"] == 3000
    assert result.payload["truncated"] is True
    assert "Returned range: 0-1200 of 3000 characters (end-exclusive)." in result.content
    assert "y" * 1200 in result.content
    assert "truncated" in result.content.lower()
    assert "search_text" in result.content


async def test_get_document_short_text_has_exact_range_and_no_truncation(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    document_id = await _doc(session, world.tenant_a, world.alice, "short.txt", "short body")
    await session.commit()

    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": str(document_id)},
    )

    assert result.ok is True
    assert result.payload["returned_range"] == [0, len("short body")]
    assert result.payload["total_length"] == len("short body")
    assert result.payload["truncated"] is False
    assert "Returned range: 0-10 of 10 characters (end-exclusive)." in result.content
    assert "truncated" not in result.content.lower()


@pytest.mark.parametrize("search_allowed", [False, True], ids=["prefix-only", "search-capable"])
@pytest.mark.parametrize("isolated_scope", [False, True], ids=["serial", "isolated"])
async def test_get_document_recovery_matches_published_assistant_catalog(
    session_and_world: tuple[AsyncSession, _World],
    monkeypatch: pytest.MonkeyPatch,
    durable_audit_ledger: RecordingDurableAuditTransactions,
    search_allowed: bool,
    isolated_scope: bool,
) -> None:
    """#610 / R1-001: follow the offered recovery without widening a frozen allowlist."""
    session, world = session_and_world
    principal = _principal(world.alice, world.tenant_a)
    excerpt = "APPROVAL CODE: 42"
    text = "x" * 2400 + excerpt
    document_id = await _doc(session, world.tenant_a, world.alice, "approval.txt", text)
    await session.commit()
    audit = AuditSink(AuditEventRepository(session, world.tenant_a))
    assistants = AssistantsService(
        session,
        tenant_id=world.tenant_a,
        owner_id=world.alice,
        roles=principal.roles,
        audit=audit,
        denials=denial_context(
            durable_audit_ledger,
            session,
            world.tenant_a,
            world.alice,
            request_id="pr616-r2",
            source_ip="127.0.0.1",
        ),
    )
    names = ("get_document", "search_text") if search_allowed else ("get_document",)
    assistant = await assistants.create(
        name="Approval reader", tool_allowlist=names, backup_owner_id=world.bob
    )
    version = await assistants.publish(assistant.id)
    config = assemble_run_config(version.config)
    assert config.allowed == frozenset(names)
    assert {spec.name for spec in tool_specs(config.allowed)} == set(names)

    def runner(allowed: frozenset[str]) -> ToolRunner:
        return ToolRunner(
            allowed=allowed,
            invocations=ToolInvocationRepository(session, world.tenant_a),
            audit=audit,
            actor=AuditActor.user(world.alice),
            request_id="pr616-r2",
            source_ip="127.0.0.1",
        )

    search_queries: list[str] = []

    async def search_text(self: RetrievalService, **kwargs: object) -> list[RetrievedPassage]:
        assert kwargs["principal"] == principal
        assert kwargs["query"] == "approval code"
        search_queries.append(str(kwargs["query"]))
        return [
            RetrievedPassage(
                chunk_id=uuid.uuid4(),
                document_id=document_id,
                document_name="approval.txt",
                ord=1,
                text=excerpt,
                char_start=2400,
                char_end=len(text),
                score=1.0,
            )
        ]

    monkeypatch.setattr(RetrievalService, "search_text", search_text)

    @asynccontextmanager
    async def scope() -> AsyncIterator[ToolContext]:
        # A separate context must receive the same authoritative catalog.
        yield _ctx(session, principal)

    governed = runner(config.allowed)
    result = await governed.run(
        call=ToolCall(
            id="prefix", name="get_document", arguments={"document_id": str(document_id)}
        ),
        context=_ctx(session, principal),
        scope=scope if isolated_scope else None,
    )
    assert result.ok is True
    assert excerpt not in result.content
    assert "Returned range: 0-2400 of 2417 characters (end-exclusive)." in result.content
    assert result.payload["truncated"] is True
    assert "characters 2400-2417 are not shown" in result.content

    if search_allowed:
        assert "use search_text with a targeted query" in result.content
    else:
        assert "This assistant cannot inspect the omitted text" in result.content
        assert "supply the relevant excerpt" in result.content
        assert "switch to an authorized search-capable assistant" in result.content
        assert "use search_text" not in result.content
        denied = await governed.run(
            call=ToolCall(id="denied", name="search_text", arguments={"query": "approval code"}),
            context=_ctx(session, principal),
        )
        assert denied.ok is False
        assert denied.error == ERROR_NOT_PERMITTED
        assert search_queries == []
        repeated = await governed.run(
            call=ToolCall(
                id="repeat", name="get_document", arguments={"document_id": str(document_id)}
            ),
            context=_ctx(session, principal),
        )
        assert repeated.content == result.content
        # Follow the suggested switch through a separately published, authorized catalog.
        recovery_assistant = await assistants.create(
            name="Search recovery",
            tool_allowlist=("get_document", "search_text"),
            backup_owner_id=world.bob,
        )
        recovery_version = await assistants.publish(recovery_assistant.id)
        governed = runner(assemble_run_config(recovery_version.config).allowed)

    recovered = await governed.run(
        call=ToolCall(id="recovery", name="search_text", arguments={"query": "approval code"}),
        context=_ctx(session, principal),
    )
    assert recovered.ok is True
    assert excerpt in recovered.content
    assert recovered.passages[0].document_id == document_id
    assert recovered.passages[0].char_start == 2400
    assert search_queries == ["approval code"]
    versions = await assistants.list_versions(assistant.id, cursor=None, limit=10)
    assert len(versions.items) == 1
    assert versions.items[0].config["toolAllowlist"] == list(names)


def test_get_document_description_conditions_search_recovery_on_availability() -> None:
    description = get_tool("get_document").description
    assert "search_text when available" in description
    assert "supply the relevant excerpt" in description


async def test_get_document_forbidden_response_discloses_no_length_or_range(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    private_text = "z" * 3000
    private_document_id = await _doc(
        session, world.tenant_a, world.bob, "private-long.txt", private_text
    )
    await session.commit()

    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": str(private_document_id)},
    )

    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert "not found" in result.content.lower()
    assert "total_length" not in result.payload
    assert "returned_range" not in result.payload
    assert "3000" not in result.content
    assert "range" not in result.content.lower()


class _RecordingRetrieval:
    """A retrieval stand-in that records the ``k`` each tool passes through."""

    def __init__(self) -> None:
        self.text_k: int | None = None
        self.docs_k: int | None = None
        self.list_k: int | None = None

    async def search_text(
        self,
        *,
        principal: object,
        query: str,
        k: int,
        collection_ids: object = None,
        document_ids: object = None,
    ) -> list:
        self.text_k = k
        return []

    async def search_documents(self, *, principal: object, name_or_query: str, k: int = 10) -> list:
        self.docs_k = k
        return []

    async def list_documents(self, *, principal: object, k: int) -> list:
        self.list_k = k
        return []


async def test_tight_budget_clamps_every_retrieval_tool(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    """#424 final re-review: a tight ``ctx.max_k`` clamps the ``k`` passed to
    search_text, search_documents (explicit AND omitted), AND list_documents —
    the whole retrieval surface degrades before the per-turn guard has to refuse."""
    _, world = session_and_world
    principal = _principal(world.alice, world.tenant_a)
    spy = _RecordingRetrieval()
    ctx = ToolContext(
        principal=principal,
        retrieval=spy,  # type: ignore[arg-type]
        collection_ids=None,
        default_k=3,
        max_k=3,  # a tight budget lowered the ceiling
    )

    await get_tool("search_text").handler({"query": "q", "k": 20}, ctx)
    assert spy.text_k == 3  # explicit k=20 hard-clamped to the tight ceiling

    await get_tool("search_documents").handler({"name_or_query": "q", "k": 20}, ctx)
    assert spy.docs_k == 3  # explicit clamped

    await get_tool("search_documents").handler({"name_or_query": "q"}, ctx)
    assert spy.docs_k == 3  # omitted default (10) also clamped down

    await get_tool("list_documents").handler({"k": 50}, ctx)
    assert spy.list_k == 3  # list_documents now honours the tight ceiling too


async def test_search_text_returns_the_complete_passage_at_any_snippet_budget(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    """A snippet budget must not truncate evidence returned to the model (#611).

    The full passage, including a fact beyond character 600, must be available in
    both the tool result and its citation-bearing passages, even when the context
    assembler supplies a smaller display budget.
    """
    from app.domain.retrieval import RetrievedPassage
    from app.services.tools.impls.retrieval import _render_passages, rendered_snippet

    # Put the late fact beyond the historical 600-character truncation point.
    text = ("evidence word " * 60).strip() + " LATE FACT: the launch date is 2031-04-19."
    passage = RetrievedPassage(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="doc.txt",
        ord=0,
        text=text,
        char_start=0,
        char_end=len(text),
        score=0.5,
    )
    assert len(text) > 600
    assert "LATE FACT: the launch date is 2031-04-19." in text[600:]

    # Keep the formatting helper's output byte-identical to the entire evidence;
    # reducing the context budget must not change the evidence itself.
    assert rendered_snippet(text, 600) == text
    assert rendered_snippet(text, 300) == text
    assert _render_passages([passage], 600).endswith(text)
    assert _render_passages([passage], 300).endswith(text)

    # Exercise the actual registered search tool: the full returned passage and
    # the model-visible snippet must both preserve the same late fact.
    class _PassageRetrieval(_RecordingRetrieval):
        async def search_text(
            self,
            *,
            principal: object,
            query: str,
            k: int,
            collection_ids: object = None,
            document_ids: object = None,
        ) -> list:
            return [passage]

    _, world = session_and_world
    for budget in (600, 300):
        ctx = ToolContext(
            principal=_principal(world.alice, world.tenant_a),
            retrieval=_PassageRetrieval(),  # type: ignore[arg-type]
            collection_ids=None,
            snippet_budget=budget,
        )
        result = await get_tool("search_text").handler({"query": "launch date"}, ctx)
        assert result.ok is True
        assert result.passages == (passage,)
        assert result.passages[0].text == text
        assert text in result.content
        assert "LATE FACT: the launch date is 2031-04-19." in result.content


# --- #758: get_document accepts what search shows ---------------------------


async def _chunk_id(session: AsyncSession, document_id: uuid.UUID) -> uuid.UUID:
    from sqlalchemy import select

    from app.db import models

    row = (
        await session.execute(
            select(models.Chunk.id).where(models.Chunk.document_id == document_id)
        )
    ).scalar_one()
    return row  # type: ignore[no-any-return]


def _outcome(result: object) -> tuple[object, ...]:
    return (
        result.content,  # type: ignore[attr-defined]
        result.ok,  # type: ignore[attr-defined]
        result.error,  # type: ignore[attr-defined]
        result.document_ids,  # type: ignore[attr-defined]
        result.payload,  # type: ignore[attr-defined]
        result.hit_count,  # type: ignore[attr-defined]
    )


def test_search_text_header_carries_document_id() -> None:
    from app.services.tools.impls.retrieval import _render_passages

    doc_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    passage = RetrievedPassage(
        chunk_id=chunk_id,
        document_id=doc_id,
        document_name="permit.pdf",
        ord=0,
        text="evidence body",
        char_start=3,
        char_end=16,
        score=1.0,
    )
    rendered = _render_passages([passage])
    assert rendered.startswith(
        f"[1] permit.pdf (document id: {doc_id}, chunk {chunk_id}, chars 3-16):"
    )
    assert f"document id: {doc_id}" in rendered

    media = RetrievedPassage(
        chunk_id=chunk_id,
        document_id=doc_id,
        document_name="meeting.mp4",
        ord=1,
        text="hello",
        char_start=0,
        char_end=5,
        score=1.0,
        time_start_ms=61_250,
        time_end_ms=64_900,
        speaker_id="speaker-2",
        speaker_name="John",
    )
    rendered_media = _render_passages([media])
    assert f"document id: {doc_id}" in rendered_media
    assert "time 01:01.250-01:04.900, John (speaker-2)" in rendered_media


async def test_get_document_accepts_chunk_uuid(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    chunk_id = await _chunk_id(session, world.alice_doc)
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": str(chunk_id)},
    )
    assert result.ok is True
    assert result.hit_count == 1
    assert result.document_ids == (world.alice_doc,)
    assert "Alice tax notes" in result.content


async def test_get_document_accepts_exact_name(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": "alice-taxes.txt"},
    )
    assert result.ok is True
    assert result.document_ids == (world.alice_doc,)
    assert "Alice tax notes" in result.content


@pytest.mark.parametrize(
    ("filename", "fallback"),
    [
        pytest.param("Document Control Plan.pdf", "Control Plan.pdf", id="document-prefix"),
        pytest.param("Chunk sizes.txt", "sizes.txt", id="chunk-prefix"),
        pytest.param("Budget (2024)", "Budget", id="parenthetical"),
        pytest.param("document: notes.md", "notes.md", id="document-colon"),
        pytest.param("Document 1.docx", "1.docx", id="word-default"),
    ],
)
async def test_get_document_literal_name_precedes_stripping(
    session_and_world: tuple[AsyncSession, _World], filename: str, fallback: str
) -> None:
    session, world = session_and_world
    literal_id = await _doc(session, world.tenant_a, world.alice, filename, "Literal body.")
    await _doc(session, world.tenant_a, world.alice, fallback, "Fallback body.")
    await session.commit()
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": filename},
    )
    assert result.ok is True
    assert result.document_ids == (literal_id,)
    assert "Literal body." in result.content
    assert "Fallback body." not in result.content


async def test_get_document_literal_ambiguity_excludes_fallback(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    literal_ids = {
        await _doc(session, world.tenant_a, world.alice, "document alice-taxes.txt", body)
        for body in ("First literal.", "Second literal.")
    }
    await session.commit()
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": "document alice-taxes.txt"},
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert result.payload == {"document_ids": [str(did) for did in sorted(literal_ids)]}
    assert str(world.alice_doc) not in result.content


@pytest.mark.parametrize(
    "handle",
    [
        pytest.param("document alice-taxes.txt", id="document-prefix"),
        pytest.param("chunk: alice-taxes.txt", id="chunk-colon"),
        pytest.param("alice-taxes.txt (copied header)", id="parenthetical"),
    ],
)
async def test_get_document_stripped_name_fallback(
    session_and_world: tuple[AsyncSession, _World], handle: str
) -> None:
    session, world = session_and_world
    result = await _call(
        session, _principal(world.alice, world.tenant_a), "get_document", {"document_id": handle}
    )
    assert result.ok is True
    assert result.document_ids == (world.alice_doc,)
    assert "Alice tax notes" in result.content


@pytest.mark.parametrize(
    "handle",
    [
        pytest.param("document bob-secret.txt", id="foreign-owner"),
        pytest.param("document carol.txt", id="cross-tenant"),
    ],
)
async def test_get_document_stripped_name_fallback_inv2(
    session_and_world: tuple[AsyncSession, _World], handle: str
) -> None:
    session, world = session_and_world
    principal = _principal(world.alice, world.tenant_a)
    unknown = await _call(session, principal, "get_document", {"document_id": "unknown.txt"})
    result = await _call(session, principal, "get_document", {"document_id": handle})
    assert unknown.ok is False
    assert unknown.error == ERROR_BAD_ARGS
    assert _outcome(result) == _outcome(unknown)


@pytest.mark.parametrize("filename", [pytest.param("Über.pdf", id="non-ascii-exact-case")])
async def test_get_document_exact_non_ascii_name(
    session_and_world: tuple[AsyncSession, _World], filename: str
) -> None:
    session, world = session_and_world
    document_id = await _doc(session, world.tenant_a, world.alice, filename, "Unicode body.")
    await session.commit()
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": filename},
    )
    assert result.ok is True
    assert result.document_ids == (document_id,)
    assert "Unicode body." in result.content


async def test_get_document_accepts_name_case_whitespace_quote_variant(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": '  "ALICE-TAXES.TXT"  '},
    )
    assert result.ok is True
    assert result.document_ids == (world.alice_doc,)
    assert "Alice tax notes" in result.content


async def test_get_document_accepts_chunk_prefix(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    chunk_id = await _chunk_id(session, world.alice_doc)
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": f"chunk {chunk_id}"},
    )
    assert result.ok is True
    assert result.document_ids == (world.alice_doc,)


async def test_get_document_accepts_document_prefix(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": f"document {world.alice_doc}"},
    )
    assert result.ok is True
    assert result.document_ids == (world.alice_doc,)


async def test_get_document_accepts_header_fragment_with_document_id(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    chunk_id = await _chunk_id(session, world.alice_doc)
    fragment = f"alice-taxes.txt (document id: {world.alice_doc}, chunk {chunk_id}, chars 0-17)"
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": fragment},
    )
    assert result.ok is True
    assert result.document_ids == (world.alice_doc,)


async def test_get_document_ambiguous_name_lists_permitted_candidates(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    dup_id = await _doc(session, world.tenant_a, world.alice, "alice-taxes.txt", "Second copy.")
    bob_dup = await _doc(session, world.tenant_a, world.bob, "alice-taxes.txt", "Bob copy.")
    await session.commit()
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": "alice-taxes.txt"},
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert str(world.alice_doc) in result.content
    assert str(dup_id) in result.content
    assert "alice-taxes.txt" in result.content
    assert "retry" in result.content.lower()
    assert str(bob_dup) not in result.content
    assert "Second copy" not in result.content


async def test_get_document_unknown_name_returns_corrective_error(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    result = await _call(
        session,
        _principal(world.alice, world.tenant_a),
        "get_document",
        {"document_id": "no-such-file.pdf"},
    )
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert result.content != "Invalid document id."
    assert "document id:" in result.content
    assert "search_text" in result.content
    assert "search_documents" in result.content
    assert "list_documents" in result.content
    assert "by filename" in result.content


async def test_get_document_malformed_value_returns_same_corrective_error(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    principal = _principal(world.alice, world.tenant_a)
    unknown = await _call(session, principal, "get_document", {"document_id": "no-such-file.pdf"})
    assert unknown.ok is False
    assert unknown.error == ERROR_BAD_ARGS
    assert "document id:" in unknown.content
    assert "search_documents" in unknown.content
    assert "by filename" in unknown.content
    for bad in ("not-a-uuid", "1", ""):
        result = await _call(session, principal, "get_document", {"document_id": bad})
        assert _outcome(result) == _outcome(unknown), bad


async def test_get_document_inv2_chunk_and_name_indistinguishable(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    principal = _principal(world.alice, world.tenant_a)
    bob_chunk = await _chunk_id(session, world.bob_doc)
    carol_chunk = await _chunk_id(session, world.carol_doc)
    unknown = await _call(session, principal, "get_document", {"document_id": str(uuid.uuid4())})
    assert unknown.ok is False
    assert unknown.error == ERROR_BAD_ARGS
    for handle in (
        str(world.bob_doc),
        str(world.carol_doc),
        str(bob_chunk),
        str(carol_chunk),
        f"chunk {bob_chunk}",
        "bob-secret.txt",
        "carol.txt",
        "document bob-secret.txt",
        "document carol.txt",
        "no-such-file.pdf",
        "not-a-uuid",
    ):
        result = await _call(session, principal, "get_document", {"document_id": handle})
        assert _outcome(result) == _outcome(unknown), handle
        assert "Bob private" not in result.content
        assert "Carol notes" not in result.content


async def test_get_document_corrective_only_names_allowed_tools(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    principal = _principal(world.alice, world.tenant_a)
    ctx = ToolContext(
        principal=principal,
        retrieval=RetrievalService(session, gateway=_FakeGateway()),  # type: ignore[arg-type]
        collection_ids=None,
        allowed_tools=frozenset({"get_document"}),
    )
    result = await get_tool("get_document").handler({"document_id": "no-such-file.pdf"}, ctx)
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert result.content.startswith("Document not found.")
    assert "chunk" in result.content
    assert "exact filename" in result.content
    assert "search_text" not in result.content
    assert "search_documents" not in result.content
    assert "list_documents" not in result.content


def test_get_document_descriptions_match_accepted_shapes() -> None:
    defn = get_tool("get_document")
    description = defn.description.lower()
    param = defn.json_schema["properties"]["document_id"]["description"].lower()
    for text in (description, param):
        assert "chunk" in text
        assert "filename" in text or "exact name" in text or "document name" in text
    assert "search_text" in description
    # Closed portable schema unchanged in shape (#634): one required string.
    assert list(defn.json_schema["properties"]) == ["document_id"]
    assert defn.json_schema["properties"]["document_id"]["type"] == "string"
    assert defn.json_schema["required"] == ["document_id"]
    assert defn.json_schema["additionalProperties"] is False
    assert "anyOf" not in str(defn.json_schema)
    assert "oneOf" not in str(defn.json_schema)
    search_text_desc = get_tool("search_text").description
    assert "document id" in search_text_desc.lower()


@pytest.mark.parametrize(
    "offline", [pytest.param(False, id="service"), pytest.param(True, id="eval")]
)
async def test_retrieval_service_resolve_document_text_shapes_and_permissions(
    session_and_world: tuple[AsyncSession, _World],
    offline: bool,
) -> None:
    from tests.eval.support import DeterministicEmbedder, OfflineRetrieval

    session, world = session_and_world
    svc = (
        OfflineRetrieval(session, embedder=DeterministicEmbedder())
        if offline
        else RetrievalService(session, gateway=_FakeGateway())  # type: ignore[arg-type]
    )
    alice = _principal(world.alice, world.tenant_a)
    chunk_id = await _chunk_id(session, world.alice_doc)
    bob_chunk = await _chunk_id(session, world.bob_doc)
    carol_chunk = await _chunk_id(session, world.carol_doc)

    by_doc = await svc.resolve_document_text(principal=alice, handle=str(world.alice_doc))
    assert by_doc.document is not None
    assert by_doc.document.document_id == world.alice_doc
    assert by_doc.candidates == ()

    by_chunk = await svc.resolve_document_text(principal=alice, handle=f"chunk {chunk_id}")
    assert by_chunk.document is not None
    assert by_chunk.document.document_id == world.alice_doc

    by_name = await svc.resolve_document_text(principal=alice, handle="  'ALICE-TAXES.TXT' ")
    assert by_name.document is not None
    assert by_name.document.document_id == world.alice_doc

    unknown = await svc.resolve_document_text(principal=alice, handle=str(uuid.uuid4()))
    assert unknown.document is None
    assert unknown.candidates == ()
    for forbidden in (
        str(world.bob_doc),
        str(world.carol_doc),
        str(bob_chunk),
        str(carol_chunk),
        "bob-secret.txt",
        "carol.txt",
        "no-such-file.pdf",
    ):
        resolved = await svc.resolve_document_text(principal=alice, handle=forbidden)
        assert resolved.document is None, forbidden
        assert resolved.candidates == (), forbidden

    dup_id = await _doc(session, world.tenant_a, world.alice, "alice-taxes.txt", "Second copy.")
    await session.commit()
    ambiguous = await svc.resolve_document_text(principal=alice, handle="alice-taxes.txt")
    assert ambiguous.document is None
    assert {c.document_id for c in ambiguous.candidates} == {world.alice_doc, dup_id}


async def test_runner_replay_get_document_argument_shapes(
    session_and_world: tuple[AsyncSession, _World],
) -> None:
    session, world = session_and_world
    principal = _principal(world.alice, world.tenant_a)
    chunk_id = await _chunk_id(session, world.alice_doc)
    runner = ToolRunner(
        allowed=default_allowlist(),
        invocations=ToolInvocationRepository(session, world.tenant_a),
        audit=AuditSink(AuditEventRepository(session, world.tenant_a)),
        actor=AuditActor.user(world.alice),
        request_id="issue-758",
        source_ip="127.0.0.1",
    )

    async def run(call_id: str, arguments: dict[str, object]):  # type: ignore[no-untyped-def]
        return await runner.run(
            call=ToolCall(id=call_id, name="get_document", arguments=arguments),
            context=_ctx(session, principal),
        )

    ok_doc = await run("ok-doc", {"document_id": str(world.alice_doc)})
    assert ok_doc.ok is True
    assert "Alice tax notes" in ok_doc.content

    ok_name = await run("ok-name", {"document_id": "alice-taxes.txt"})
    assert ok_name.ok is True
    assert "Alice tax notes" in ok_name.content

    ok_chunk = await run("ok-chunk", {"document_id": str(chunk_id)})
    assert ok_chunk.ok is True
    assert "Alice tax notes" in ok_chunk.content

    ok_chunk_prefix = await run("ok-chunk-prefix", {"document_id": f"chunk {chunk_id}"})
    assert ok_chunk_prefix.ok is True

    bad_index = await run("bad-index", {"document_id": "1"})
    assert bad_index.ok is False
    assert bad_index.error == ERROR_BAD_ARGS
    assert "document id:" in bad_index.content
    assert "search_documents" in bad_index.content

    bad_int = await run("bad-int", {"document_id": 1})
    assert bad_int.ok is False
    assert bad_int.error == ERROR_BAD_ARGS
    assert "must satisfy type string" in bad_int.content
    assert "Correct the arguments and retry" in bad_int.content

    for i, key in enumerate(("document_name", "name", "id", "chunk_id", "documentId")):
        rejected = await run(f"bad-key-{i}", {key: "alice-taxes.txt"})
        assert rejected.ok is False, key
        assert rejected.error == ERROR_BAD_ARGS, key
        assert "Correct the arguments and retry" in rejected.content, key
        assert "unknown" in rejected.content or "required" in rejected.content, key
