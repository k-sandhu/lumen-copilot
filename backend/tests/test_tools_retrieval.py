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

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import Embedding
from app.domain.tools import ERROR_BAD_ARGS, RiskTier
from app.retrieval import RetrievalService
from app.services.tools.registry import default_allowlist, get_tool, registered_names, tool_specs
from app.services.tools.types import ToolContext

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_EMBED_DIM = 1024


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
            await conn.run_sync(Base.metadata.create_all)
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
        status=DocumentStatus.READY,
    )
    await ChunkRepository(session, tenant_id).replace_for_document(
        doc.id, [ChunkInput(text=text, char_start=0, char_end=len(text))]
    )
    return doc.id


def _ctx(session: AsyncSession, principal: Principal) -> ToolContext:
    service = RetrievalService(session, gateway=_FakeGateway())  # type: ignore[arg-type]
    return ToolContext(principal=principal, retrieval=service, collection_ids=None, default_k=6)


async def _call(session: AsyncSession, principal: Principal, name: str, args: dict[str, object]):
    """Invoke a registered tool's handler directly (unit-level, no runner)."""
    handler = get_tool(name).handler
    return await handler(dict(args), _ctx(session, principal))


# --- Registry discovery + governance metadata (AC-1) ------------------------


_RETRIEVAL_TOOLS = frozenset(
    {"search_text", "search_documents", "list_documents", "get_document"}
)


def test_registry_discovers_the_retrieval_tools() -> None:
    assert _RETRIEVAL_TOOLS <= registered_names()


def test_retrieval_tools_are_t0_read_only_no_approval() -> None:
    for name in _RETRIEVAL_TOOLS:
        defn = get_tool(name)
        assert defn.risk_tier is RiskTier.T0
        assert defn.read_only is True
        assert defn.requires_approval is False


def test_default_allowlist_is_the_read_only_retrieval_tools() -> None:
    # Ad-hoc chat's default allow-list = the read-only retrieval tools, now four:
    # list_documents (T0/read-only/default_offered) auto-joins on discovery (#371).
    assert default_allowlist() == _RETRIEVAL_TOOLS


def test_tool_specs_render_the_allowlist_to_llm_specs() -> None:
    specs = tool_specs(default_allowlist())
    names = {s.name for s in specs}
    assert names == _RETRIEVAL_TOOLS
    # Each spec carries the JSON-Schema parameters the model fills in.
    by_name = {s.name: s for s in specs}
    assert by_name["search_text"].parameters["required"] == ["query"]
    # list_documents is a pure enumeration — no required args (no query needed).
    assert by_name["list_documents"].parameters.get("required", []) == []


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
    # crash: the handler returns ok=False with the bad-args code.
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert "invalid" in result.content.lower()
