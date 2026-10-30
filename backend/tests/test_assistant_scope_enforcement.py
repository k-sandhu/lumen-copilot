"""Server enforcement of an assistant's published knowledge scope (#581 part 1).

Regression coverage for the scope-server lane:

* effective modes + the ``web_search`` coupling in ``assistant_runtime``;
* legacy retrieval tools and ``retrieval/`` honouring collection/source scope
  (an out-of-scope document is indistinguishable from a missing one, INV-2);
* corpus tools narrowing by ``source_ids``;
* configure-time rejection of ``web_search`` without the ``web`` mode;
* fail-closed send for a session bound to an assistant whose pinned config
  can no longer be loaded (never a silent fall back to unrestricted chat);
* answer-audit provenance (assistant id/version + effective scope, INV-6).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql import Select

from app.auth.principal import Principal
from app.core.config import CANONICAL_EMBEDDING_DIMENSIONS, get_settings
from app.core.errors import ValidationError
from app.db import models as db_models
from app.db.repositories import (
    AssistantRepository,
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    MessageRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import AutonomyLevel, DocumentStatus, KnowledgeMode, KnowledgeScope, Role
from app.domain.llm import Embedding, StreamEvent, ToolCall
from app.realtime.backplane import InMemoryBackplane
from app.retrieval import RetrievalService
from app.search import SearchHit
from app.services.assistant_runtime import assemble_run_config, effective_modes
from app.services.assistants_service import AssistantsService
from app.services.audit import AuditSink
from app.services.chat_runtime import ChatRuntime
from app.services.chat_service import ChatService
from app.services.prompts import GROUNDED_SYSTEM_PROMPT, NO_SOURCES_FALLBACK
from app.services.tools.handles import EvidenceHandles
from app.services.tools.registry import get_tool
from app.services.tools.types import ToolContext
from tests._audit_helpers import (
    RecordingDurableAuditTransactions,
    denial_context_from_session,
)
from tests._db_helpers import copy_sqlite_schema
from tests._retrieval_helpers import UUIDDocumentResolver

import app.db.models  # noqa: F401  isort: skip

_EMBED_DIM = CANONICAL_EMBEDDING_DIMENSIONS


class _FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(
        self, inputs: list[str], *, cache_namespace: str | None = None
    ) -> list[Embedding]:
        self.calls += 1
        return [Embedding(vector=[0.0] * _EMBED_DIM, model="fake") for _ in inputs]


# ---------------------------------------------------------------------------
# Unit: effective modes + web_search coupling (assistant_runtime).
# ---------------------------------------------------------------------------


def _config(*, modes: list[str], allowlist: list[str], source_ids: list[str] | None = None) -> dict:
    return {
        "name": "Scoped",
        "instructions": "Be scoped.",
        "model": None,
        "knowledgeScope": {
            "collectionIds": [],
            "sourceIds": source_ids or [],
            "modes": modes,
        },
        "toolAllowlist": allowlist,
        "autonomyLevel": "suggest",
    }


def test_effective_modes_empty_legacy_backfills_web_from_allowlist() -> None:
    modes = effective_modes({"modes": []}, ["web_search", "search_text"])
    assert modes == (KnowledgeMode.COMPANY, KnowledgeMode.UPLOADED, KnowledgeMode.WEB)


def test_effective_modes_empty_without_web_tool_has_no_web() -> None:
    modes = effective_modes({"modes": []}, ["search_text"])
    assert modes == (KnowledgeMode.COMPANY, KnowledgeMode.UPLOADED)


def test_effective_modes_non_empty_is_authoritative() -> None:
    modes = effective_modes({"modes": ["company"]}, ["web_search"])
    assert modes == (KnowledgeMode.COMPANY,)


def test_web_search_dropped_when_modes_lack_web() -> None:
    cfg = assemble_run_config(
        _config(modes=["company", "uploaded"], allowlist=["web_search", "search_text"])
    )
    assert "web_search" not in cfg.allowed
    assert "search_text" in cfg.allowed
    assert cfg.modes == (KnowledgeMode.COMPANY, KnowledgeMode.UPLOADED)


def test_web_search_kept_for_legacy_empty_modes() -> None:
    cfg = assemble_run_config(_config(modes=[], allowlist=["web_search"]))
    assert "web_search" in cfg.allowed
    assert KnowledgeMode.WEB in cfg.modes


def test_web_search_kept_when_web_mode_present() -> None:
    cfg = assemble_run_config(_config(modes=["web"], allowlist=["web_search"]))
    assert "web_search" in cfg.allowed


def test_model_mode_preserves_grounding_and_cannot_enable_web() -> None:
    config = assemble_run_config(_config(modes=["model"], allowlist=["web_search", "search_text"]))
    assert config.modes == (KnowledgeMode.MODEL,)
    assert "web_search" not in config.allowed
    assert GROUNDED_SYSTEM_PROMPT in config.system_prompt


def test_assemble_carries_source_ids_and_provenance() -> None:
    source_id = uuid.uuid4()
    assistant_id, version_id = uuid.uuid4(), uuid.uuid4()
    cfg = assemble_run_config(
        _config(modes=["company"], allowlist=["search_text"], source_ids=[str(source_id)]),
        assistant_id=assistant_id,
        assistant_version_id=version_id,
    )
    assert cfg.source_ids == [source_id]
    assert cfg.assistant_id == assistant_id
    assert cfg.assistant_version_id == version_id


# ---------------------------------------------------------------------------
# Integration fixtures: one tenant, two collections, two sources, three docs.
# ---------------------------------------------------------------------------


class _World:
    def __init__(self, **kwargs: uuid.UUID) -> None:
        self.__dict__.update(kwargs)


@pytest_asyncio.fixture
async def factory(
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(copy_sqlite_schema)
        yield async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            info={"durable_audit_ledger": durable_audit_ledger},
        )
    finally:
        await engine.dispose()


async def _doc(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    collection_id: uuid.UUID,
    source_id: uuid.UUID | None,
    filename: str,
    text: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    doc = await DocumentRepository(session, tenant_id).create(
        owner_id=owner_id,
        collection_id=collection_id,
        filename=filename,
        mime_type="text/plain",
        size_bytes=len(text),
        storage_key=f"{tenant_id}/{filename}",
        acl_enforced=False,
        status=DocumentStatus.READY,
        source_id=source_id,
    )
    chunks = await ChunkRepository(session, tenant_id).replace_for_document(
        doc.id,
        [ChunkInput(text=text, char_start=0, char_end=len(text), embedding_fingerprint="b" * 64)],
    )
    return doc.id, chunks[0].id


@pytest_asyncio.fixture
async def world(factory: async_sessionmaker[AsyncSession]) -> _World:
    async with factory() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        alice = await UserRepository(session, tenant.id).create(
            email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
        )
        bob = await UserRepository(session, tenant.id).create(
            email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
        )
        coll_a = await CollectionRepository(session, tenant.id).create(owner_id=alice.id, name="A")
        coll_b = await CollectionRepository(session, tenant.id).create(owner_id=alice.id, name="B")
        src_1 = await SourceRepository(session, tenant.id).create(
            owner_id=alice.id, type="web", config={}
        )
        src_2 = await SourceRepository(session, tenant.id).create(
            owner_id=alice.id, type="web", config={}
        )
        doc_a_s1, chunk_a = await _doc(
            session, tenant.id, alice.id, coll_a.id, src_1.id, "alpha-report.txt", "Alpha body."
        )
        doc_b_s2, _chunk_b = await _doc(
            session, tenant.id, alice.id, coll_b.id, src_2.id, "beta-report.txt", "Beta body."
        )
        doc_a_up, _chunk_u = await _doc(
            session, tenant.id, alice.id, coll_a.id, None, "alpha-upload.txt", "Upload body."
        )
        await session.commit()
        return _World(
            tenant=tenant.id,
            alice=alice.id,
            bob=bob.id,
            coll_a=coll_a.id,
            coll_b=coll_b.id,
            src_1=src_1.id,
            src_2=src_2.id,
            doc_a_s1=doc_a_s1,
            doc_b_s2=doc_b_s2,
            doc_a_up=doc_a_up,
            chunk_a=chunk_a,
        )


def _principal(world: _World) -> Principal:
    return Principal(user_id=world.alice, tenant_id=world.tenant, roles=(Role.MEMBER,))


def _ctx(
    session: AsyncSession,
    world: _World,
    *,
    collection_ids: list[uuid.UUID] | None = None,
    source_ids: list[uuid.UUID] | None = None,
) -> ToolContext:
    service = RetrievalService(session, gateway=_FakeGateway())  # type: ignore[arg-type]
    return ToolContext(
        principal=_principal(world),
        retrieval=service,
        collection_ids=collection_ids,
        source_ids=source_ids,
        default_k=6,
        allowed_tools=frozenset(
            {
                "search_text",
                "search_documents",
                "list_documents",
                "get_document",
                "search_passages",
                "find_documents",
                "read_document",
            }
        ),
    )


async def _call(ctx: ToolContext, name: str, args: dict[str, object]):
    return await get_tool(name).handler(dict(args), ctx)


# ---------------------------------------------------------------------------
# Legacy + corpus tools honour collection/source scope (INV-2 narrowing).
# ---------------------------------------------------------------------------


async def test_legacy_tools_honour_collection_scope(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        ctx = _ctx(session, world, collection_ids=[world.coll_a])
        found = await _call(ctx, "search_documents", {"name_or_query": "report"})
        assert set(found.document_ids) == {world.doc_a_s1}
        listed = await _call(ctx, "list_documents", {})
        assert set(listed.document_ids) == {world.doc_a_s1, world.doc_a_up}
        # Out-of-scope document: same not-found outcome as a missing id.
        out = await _call(ctx, "get_document", {"document_id": str(world.doc_b_s2)})
        missing = await _call(ctx, "get_document", {"document_id": str(uuid.uuid4())})
        assert out.ok is False and missing.ok is False
        assert out.content == missing.content
        assert out.error == missing.error
        # Exact-name resolution is scoped too (no existence leak via filename).
        by_name = await _call(ctx, "get_document", {"document_id": "beta-report.txt"})
        assert by_name.ok is False
        assert by_name.content == missing.content
        inside = await _call(ctx, "get_document", {"document_id": str(world.doc_a_s1)})
        assert inside.ok is True
        assert "Alpha body." in inside.content


async def test_legacy_and_corpus_tools_honour_source_scope(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        ctx = _ctx(session, world, source_ids=[world.src_1])
        listed = await _call(ctx, "list_documents", {})
        assert set(listed.document_ids) == {world.doc_a_s1}
        found = await _call(ctx, "search_documents", {"name_or_query": "alpha"})
        assert set(found.document_ids) == {world.doc_a_s1}
        corpus = await _call(ctx, "find_documents", {"query": "alpha"})
        assert set(corpus.document_ids) == {world.doc_a_s1}
        out = await _call(ctx, "get_document", {"document_id": str(world.doc_a_up)})
        assert out.ok is False
        read = await _call(ctx, "read_document", {"handle": str(world.doc_b_s2)})
        assert read.ok is False
        read_in = await _call(ctx, "read_document", {"handle": str(world.doc_a_s1)})
        assert read_in.ok is True


@pytest.mark.parametrize("axis", ["collection", "source"])
@pytest.mark.parametrize("handle", ["uuid", "chunk", "name"])
async def test_out_of_scope_handles_are_indistinguishable_from_missing(
    factory: async_sessionmaker[AsyncSession], world: _World, axis: str, handle: str
) -> None:
    async with factory() as session:
        ctx = _ctx(
            session,
            world,
            collection_ids=[world.coll_a] if axis == "collection" else None,
            source_ids=[world.src_1] if axis == "source" else None,
        )
        chunks = await ChunkRepository(session, world.tenant).list_for_document(world.doc_b_s2)
        value = {
            "uuid": str(world.doc_b_s2),
            "chunk": str(chunks[0].id),
            "name": "beta-report.txt",
        }[handle]
        out = await _call(ctx, "get_document", {"document_id": value})
        missing = await _call(ctx, "get_document", {"document_id": str(uuid.uuid4())})
        assert not out.ok
        assert (out.content, out.error) == (missing.content, missing.error)


async def test_retrieval_service_source_scope_direct(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        service = RetrievalService(session, gateway=_FakeGateway())  # type: ignore[arg-type]
        principal = _principal(world)
        matches = await service.search_documents(
            principal=principal, name_or_query="report", k=10, source_ids=[world.src_2]
        )
        assert [m.document_id for m in matches] == [world.doc_b_s2]
        assert (
            await service.get_document(
                principal=principal, document_id=world.doc_a_s1, source_ids=[world.src_2]
            )
            is None
        )
        passages = await service.read_passages(
            principal=principal, chunk_ids=[world.chunk_a], source_ids=[world.src_2]
        )
        assert passages == []
        passages_in = await service.read_passages(
            principal=principal, chunk_ids=[world.chunk_a], source_ids=[world.src_1]
        )
        assert [p.document_id for p in passages_in] == [world.doc_a_s1]


async def test_retrieval_search_empty_source_scope_short_circuits_before_any_model_call(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    """``search`` threads ``source_ids`` into its traced body (#818's RETRIEVER span split).

    A source scope that resolves to no permitted document is the strictest
    narrowing: no passages, and neither the embedding model nor the engine is
    called. Guards the merge of #581's scope into the observability refactor,
    where the scope was dropped from the helper's signature.
    """
    async with factory() as session:
        gateway = _FakeGateway()
        service = RetrievalService(session, gateway=gateway)  # type: ignore[arg-type]
        passages = await service.search(
            principal=_principal(world), query="report", k=10, source_ids=[uuid.uuid4()]
        )
        assert passages == []
        assert gateway.calls == 0


@pytest.mark.parametrize("tool", ["find_documents", "search_passages", "read_document"])
@pytest.mark.parametrize("axis", ["collection", "source"])
@pytest.mark.parametrize("identity", ["uuid", "handle"])
async def test_corpus_explicit_document_filters_reject_excluded_identities(
    factory: async_sessionmaker[AsyncSession], world: _World, tool: str, axis: str, identity: str
) -> None:
    """R1-001: explicit excluded and missing identities have the same unavailable result."""
    async with factory() as session:
        ctx = _ctx(
            session,
            world,
            collection_ids=[world.coll_a] if axis == "collection" else None,
            source_ids=[world.src_1] if axis == "source" else None,
        )
        ctx = replace(
            ctx,
            handles=EvidenceHandles(),
            retrieval=RetrievalService(
                session,
                gateway=_FakeGateway(),  # type: ignore[arg-type]
                store=_SearchStore(  # type: ignore[arg-type]
                    [SearchHit(world.chunk_a, world.doc_a_s1, 1.0, 0, "b" * 64)]
                ),
            ),
        )
        excluded = (
            ctx.handles.document(world.doc_b_s2) if identity == "handle" else str(world.doc_b_s2)
        )
        missing = str(uuid.uuid4())
        args = {"query": "Alpha", "handle": str(world.doc_a_s1)}
        out = await _call(ctx, tool, {**args, "document_ids": [excluded]})
        absent = await _call(ctx, tool, {**args, "document_ids": [missing]})
        assert not out.ok and not absent.ok
        assert out.error == absent.error == "tool_bad_args"
        # The field error echoes only the caller's own input, not document metadata.
        assert out.content.replace(repr(excluded), "<input>") == absent.content.replace(
            repr(missing), "<input>"
        )
        assert not out.document_ids and not out.passages
        allowed = await _call(ctx, tool, {**args, "document_ids": [str(world.doc_a_s1)]})
        assert allowed.ok
        assert world.doc_a_s1 in allowed.document_ids


@pytest.mark.parametrize("axis", ["source", "collection", "permission"])
@pytest.mark.parametrize("start", [0, 100])
async def test_corpus_range_metadata_rechecks_current_scope(
    factory: async_sessionmaker[AsyncSession],
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    axis: str,
    start: int,
) -> None:
    """R1-002: move/revoke exactly at the SQL length-read boundary, including empty ranges."""
    async with factory() as session:
        execute = session.execute
        changed = False

        async def move_before_length_read(statement, *args, **kwargs):
            nonlocal changed
            if (
                not changed
                and isinstance(statement, Select)
                and "max(chunks.char_end)" in str(statement)
            ):
                changed = True
                await execute(
                    sa_update(db_models.Document)
                    .where(db_models.Document.id == world.doc_a_s1)
                    .values(
                        **{
                            "source": {"source_id": world.src_2},
                            "collection": {"collection_id": world.coll_b},
                            "permission": {"owner_id": world.bob},
                        }[axis]
                    )
                )
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", move_before_length_read)
        ctx = _ctx(session, world, collection_ids=[world.coll_a], source_ids=[world.src_1])
        result = await _call(ctx, "read_document", {"handle": str(world.doc_a_s1), "start": start})
        assert changed, "the test must change access at the length-read boundary"
        assert not result.ok
        missing = await _call(ctx, "read_document", {"handle": str(uuid.uuid4()), "start": start})
        assert (result.error, result.content) == (missing.error, missing.content)
        assert not result.document_ids and not result.passages


@pytest.mark.parametrize("empty", [False, True])
async def test_corpus_range_metadata_allows_available_empty_ranges(
    factory: async_sessionmaker[AsyncSession], world: _World, empty: bool
) -> None:
    """An available document with no matching passages still has valid range metadata."""
    async with factory() as session:
        if empty:
            await session.execute(
                sa_delete(db_models.Chunk).where(db_models.Chunk.document_id == world.doc_a_s1)
            )
        ctx = _ctx(session, world, collection_ids=[world.coll_a], source_ids=[world.src_1])
        result = await _call(ctx, "read_document", {"handle": str(world.doc_a_s1), "start": 100})
        assert result.ok and not result.passages
        assert f"Available range: 0-{0 if empty else len('Alpha body.')}" in result.content


@pytest.mark.parametrize("axis", ["collection", "source"])
async def test_legacy_read_checks_scope_in_the_content_query(
    factory: async_sessionmaker[AsyncSession],
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    axis: str,
) -> None:
    async with factory() as session:
        execute = session.execute
        changed = False

        async def move_before_content_read(statement, *args, **kwargs):
            nonlocal changed
            if isinstance(statement, Select) and any(
                column.shares_lineage(db_models.Chunk.text) for column in statement.selected_columns
            ):
                changed = True
                await execute(
                    sa_update(db_models.Document)
                    .where(db_models.Document.id == world.doc_a_s1)
                    .values(
                        **(
                            {"collection_id": world.coll_b}
                            if axis == "collection"
                            else {"source_id": world.src_2}
                        )
                    )
                )
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", move_before_content_read)
        service = RetrievalService(session, gateway=_FakeGateway())  # type: ignore[arg-type]
        result = await service.get_document(
            principal=_principal(world),
            document_id=world.doc_a_s1,
            collection_ids=[world.coll_a] if axis == "collection" else None,
            source_ids=[world.src_1] if axis == "source" else None,
        )
        assert changed, "the test must move the document at the content-read boundary"
        assert result is None


class _SearchStore:
    """Record scope but deliberately return all hits: SQL must recheck them."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.calls: list[dict[str, object]] = []

    async def ensure_index(self) -> None:
        pass

    async def hybrid_search(self, **kwargs: object) -> list[SearchHit]:
        self.calls.append(kwargs)
        return self.hits


@pytest.mark.parametrize("tool", ["search_text", "search_passages"])
async def test_source_scope_reaches_engine_and_sql_hydration(
    factory: async_sessionmaker[AsyncSession], world: _World, tool: str
) -> None:
    async with factory() as session:
        chunks = await ChunkRepository(session, world.tenant).list_for_document(world.doc_b_s2)
        store = _SearchStore(
            [
                SearchHit(world.chunk_a, world.doc_a_s1, 1.0, 0, "b" * 64),
                SearchHit(chunks[0].id, world.doc_b_s2, 0.5, 0, "b" * 64),
            ]
        )
        ctx = _ctx(session, world, source_ids=[world.src_1])
        ctx = replace(ctx, retrieval=RetrievalService(session, gateway=_FakeGateway(), store=store))  # type: ignore[arg-type]
        result = await _call(ctx, tool, {"query": "report"})
        assert result.ok
        assert [p.document_id for p in result.passages] == [world.doc_a_s1]
        assert store.calls[0]["document_ids"] == [world.doc_a_s1]


@pytest.mark.parametrize("pinned", [False, True])
async def test_disjoint_source_scope_stops_before_embedding_or_engine(
    factory: async_sessionmaker[AsyncSession], world: _World, pinned: bool
) -> None:
    async with factory() as session:
        gateway = _FakeGateway()
        store = _SearchStore([])
        service = RetrievalService(session, gateway=gateway, store=store)  # type: ignore[arg-type]
        result = await service.search_text(
            principal=_principal(world),
            query="report",
            k=6,
            source_ids=[world.src_1] if pinned else [],
            document_ids=[world.doc_b_s2] if pinned else None,
        )
        assert result == []
        assert gateway.calls == 0
        assert store.calls == []


@pytest.mark.parametrize("axis", ["collection", "source"])
async def test_scope_intersects_permissions_and_handle_resolution(
    factory: async_sessionmaker[AsyncSession], world: _World, axis: str
) -> None:
    async with factory() as session:
        ctx = _ctx(
            session,
            world,
            collection_ids=[world.coll_a] if axis == "collection" else None,
            source_ids=[world.src_1] if axis == "source" else None,
        )
        # A permitted source/collection is a filter, never a grant to its documents.
        await session.execute(
            sa_update(db_models.Document)
            .where(db_models.Document.id == world.doc_a_s1)
            .values(owner_id=world.bob)
        )
        await session.commit()
        for handle in (str(world.doc_a_s1), str(world.chunk_a), "alpha-report.txt"):
            out = await _call(ctx, "get_document", {"document_id": handle})
            missing = await _call(ctx, "get_document", {"document_id": str(uuid.uuid4())})
            assert (out.ok, out.content, out.error) == (missing.ok, missing.content, missing.error)
        listed = await _call(ctx, "list_documents", {})
        assert world.doc_a_s1 not in listed.document_ids


async def test_scoped_discovery_cursor_cannot_be_reused_for_another_source(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        await _doc(
            session, world.tenant, world.alice, world.coll_a, world.src_1, "another.txt", "another"
        )
        service = RetrievalService(session, gateway=_FakeGateway())  # type: ignore[arg-type]
        page = await service.find_documents(
            principal=_principal(world), limit=1, source_ids=[world.src_1]
        )
        assert page.next_cursor is not None
        with pytest.raises(ValidationError):
            await service.find_documents(
                principal=_principal(world),
                limit=1,
                cursor=page.next_cursor,
                source_ids=[world.src_2],
            )


# ---------------------------------------------------------------------------
# Configure-time validation: web_search requires the web mode (#581 item 5).
# ---------------------------------------------------------------------------


def _assistants(session: AsyncSession, world: _World) -> AssistantsService:
    return AssistantsService(
        session,
        tenant_id=world.tenant,
        owner_id=world.alice,
        roles=(Role.MEMBER,),
        audit=AuditSink(AuditEventRepository(session, world.tenant)),
        denials=denial_context_from_session(
            session, world.tenant, world.alice, request_id="req-scope", source_ip="203.0.113.1"
        ),
    )


async def test_create_rejects_web_search_without_web_mode(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        svc = _assistants(session, world)
        with pytest.raises(ValidationError) as excinfo:
            await svc.create(
                name="Bad web",
                knowledge_scope=KnowledgeScope(modes=(KnowledgeMode.COMPANY,)),
                tool_allowlist=("web_search",),
            )
        assert excinfo.value.code == "web_search_requires_web_mode"
        assert excinfo.value.status == 422


async def test_create_allows_web_search_with_web_mode_and_legacy_empty_modes(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        svc = _assistants(session, world)
        with_web = await svc.create(
            name="Web ok",
            knowledge_scope=KnowledgeScope(modes=(KnowledgeMode.WEB,)),
            tool_allowlist=("web_search",),
        )
        assert with_web.id is not None
        legacy = await svc.create(
            name="Legacy ok",
            knowledge_scope=KnowledgeScope(modes=()),
            tool_allowlist=("web_search",),
        )
        assert legacy.id is not None


async def test_publish_rejects_web_search_without_web_mode(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        # Seed an inconsistent head directly (bypassing service validation, as a
        # pre-fix row would exist), then publish through the service.
        row = await AssistantRepository(session, world.tenant).create(
            owner_id=world.alice,
            name="Seeded bad",
            description=None,
            instructions=None,
            model=None,
            knowledge_scope=KnowledgeScope(modes=(KnowledgeMode.COMPANY,)),
            tool_allowlist=("web_search",),
            autonomy_level=AutonomyLevel.SUGGEST,
            backup_owner_id=world.bob,
        )
        await session.commit()
        svc = _assistants(session, world)
        with pytest.raises(ValidationError) as excinfo:
            await svc.publish(row.id)
        assert excinfo.value.code == "web_search_requires_web_mode"


@pytest.mark.parametrize("change", ["modes", "tools"])
async def test_update_validates_resulting_web_mode_and_tools(
    factory: async_sessionmaker[AsyncSession], world: _World, change: str
) -> None:
    async with factory() as session:
        service = _assistants(session, world)
        assistant = await service.create(
            name="Valid",
            knowledge_scope=KnowledgeScope(modes=(KnowledgeMode.WEB,)),
            tool_allowlist=("web_search",) if change == "modes" else (),
        )
        if change == "tools":
            await service.update(
                assistant.id, knowledge_scope=KnowledgeScope(modes=(KnowledgeMode.COMPANY,))
            )
        with pytest.raises(ValidationError) as excinfo:
            if change == "modes":
                await service.update(
                    assistant.id, knowledge_scope=KnowledgeScope(modes=(KnowledgeMode.COMPANY,))
                )
            else:
                await service.update(assistant.id, tool_allowlist=["web_search"])
        assert excinfo.value.code == "web_search_requires_web_mode"


async def test_competing_partial_updates_validate_the_locked_current_head(
    factory: async_sessionmaker[AsyncSession], world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1-003: two sessions compete after A's read; model row locking for offline SQLite.

    SQLite ignores FOR UPDATE. The execute seam implements that statement's row
    lock semantics, held through commit/rollback, and otherwise lets B commit
    between A's read and validation. No scheduler timing or real sockets are used.
    """
    async with factory() as setup:
        assistant = await _assistants(setup, world).create(
            name="Competing patches", knowledge_scope=KnowledgeScope(modes=(KnowledgeMode.WEB,))
        )
        await setup.commit()
    async with factory() as first, factory() as second:
        # Keep an old ORM identity alive: the locked read must refresh it as well.
        stale = (await second.execute(sa.select(db_models.Assistant))).scalar_one()
        first_loaded = asyncio.Event()
        second_attempted = asyncio.Event()
        second_finished = asyncio.Event()
        row_lock = asyncio.Lock()
        held: set[AsyncSession] = set()
        locked_reads: list[AsyncSession] = []

        def wrap(session: AsyncSession, is_first: bool):
            execute = session.execute
            read = False

            async def execute_with_lock(statement, *args, **kwargs):
                nonlocal read
                is_head = isinstance(statement, Select) and any(
                    item.get("entity") is db_models.Assistant
                    for item in statement.column_descriptions
                )
                if not is_head or read:
                    return await execute(statement, *args, **kwargs)
                read = True
                locked = statement._for_update_arg is not None
                if not is_first:
                    second_attempted.set()
                if locked:
                    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
                    await row_lock.acquire()
                    held.add(session)
                    locked_reads.append(session)
                result = await execute(statement, *args, **kwargs)
                if is_first:
                    first_loaded.set()
                    await second_attempted.wait()
                    if not locked:
                        await second_finished.wait()
                return result

            monkeypatch.setattr(session, "execute", execute_with_lock)

        wrap(first, True)
        wrap(second, False)

        async def patch(session: AsyncSession, fields: dict[str, object]):
            try:
                result = await _assistants(session, world).update(assistant.id, **fields)
                await session.commit()
                return result
            except ValidationError as exc:
                await session.rollback()
                return exc
            finally:
                if session in held:
                    held.remove(session)
                    row_lock.release()

        async def patch_tools():
            await first_loaded.wait()
            try:
                return await patch(second, {"tool_allowlist": ["web_search"]})
            finally:
                second_finished.set()

        mode_result, tool_result = await asyncio.gather(
            patch(first, {"knowledge_scope": KnowledgeScope(modes=(KnowledgeMode.COMPANY,))}),
            patch_tools(),
        )
        assert not isinstance(mode_result, ValidationError)
        assert isinstance(tool_result, ValidationError)
        assert tool_result.code == "web_search_requires_web_mode"
        assert locked_reads == [first, second]
        assert sa.inspect(stale).identity == (assistant.id,)
    async with factory() as check:
        current = await AssistantRepository(check, world.tenant).get(assistant.id)
        assert current.knowledge_scope.modes == (KnowledgeMode.COMPANY,)
        assert current.tool_allowlist == ()


# ---------------------------------------------------------------------------
# Fail closed: a session bound to an assistant whose config is gone (#581 item 4).
# ---------------------------------------------------------------------------


def _chat(session: AsyncSession, world: _World) -> ChatService:
    return ChatService(
        session,
        tenant_id=world.tenant,
        owner_id=world.alice,
        settings=get_settings(),
        denials=denial_context_from_session(
            session, world.tenant, world.alice, request_id="req-scope", source_ip="203.0.113.1"
        ),
    )


async def _published_session(factory: async_sessionmaker[AsyncSession], world: _World) -> uuid.UUID:
    async with factory() as session:
        assistants = _assistants(session, world)
        created = await assistants.create(name="Bound", backup_owner_id=world.bob)
        await assistants.publish(created.id)
        chat = _chat(session, world)
        view = await chat.create_session(title="t", model=None, assistant_id=created.id)
        await session.commit()
        return view.session.id


async def test_send_fails_closed_when_pinned_version_deleted(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    session_id = await _published_session(factory, world)
    async with factory() as session:
        await session.execute(
            sa_delete(db_models.AssistantVersion).where(
                db_models.AssistantVersion.tenant_id == world.tenant
            )
        )
        await session.commit()
        chat = _chat(session, world)
        with pytest.raises(ValidationError) as excinfo:
            await chat.send_message(
                session_id, content="hi", model=None, backplane=InMemoryBackplane()
            )
        assert excinfo.value.code == "assistant_config_unavailable"
        assert excinfo.value.status == 422
        # Nothing was persisted for the refused turn.
        view = await chat.list_messages(session_id, cursor=None, limit=10)
        assert view is not None
        assert view.items == []


async def test_send_fails_closed_after_assistant_deletion_nulls_fks(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    session_id = await _published_session(factory, world)
    async with factory() as session:
        # Simulate ON DELETE SET NULL after the assistant (and its versions) go.
        await session.execute(
            sa_update(db_models.ChatSession)
            .where(db_models.ChatSession.id == session_id)
            .values(assistant_id=None, assistant_version_id=None)
        )
        await session.commit()
        chat = _chat(session, world)
        with pytest.raises(ValidationError) as excinfo:
            await chat.send_message(
                session_id, content="hi", model=None, backplane=InMemoryBackplane()
            )
        assert excinfo.value.code == "assistant_config_unavailable"
        assert excinfo.value.status == 422


async def test_assistant_bound_marker_set_only_for_assistant_sessions(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    bound_id = await _published_session(factory, world)
    async with factory() as session:
        chat = _chat(session, world)
        adhoc = await chat.create_session(title="adhoc", model=None)
        await session.commit()
        bound = await chat.get_session(bound_id)
        assert bound is not None and bound.session.assistant_bound is True
        assert adhoc.session.assistant_bound is False


async def test_send_loads_pinned_scope_and_provenance_after_head_changes(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    async with factory() as session:
        assistants = _assistants(session, world)
        assistant = await assistants.create(
            name="Pinned",
            backup_owner_id=world.bob,
            tool_allowlist=("search_text",),
            knowledge_scope=KnowledgeScope(
                collection_ids=(world.coll_a,),
                source_ids=(world.src_1,),
                modes=(KnowledgeMode.COMPANY,),
            ),
        )
        version = await assistants.publish(assistant.id)
        chat = _chat(session, world)
        bound = await chat.create_session(title="bound", model=None, assistant_id=assistant.id)
        await assistants.update(
            assistant.id,
            knowledge_scope=KnowledgeScope(
                collection_ids=(world.coll_b,),
                source_ids=(world.src_2,),
                modes=(KnowledgeMode.WEB,),
            ),
        )
        result = await chat.send_message(
            bound.session.id, content="hi", model=None, backplane=InMemoryBackplane()
        )
        assert result is not None
        config = result.assistant_config
        assert config is not None
        assert config.assistant_id == assistant.id
        assert config.assistant_version_id == version.id
        assert config.collection_ids == [world.coll_a]
        assert config.source_ids == [world.src_1]
        assert config.modes == (KnowledgeMode.COMPANY,)


def test_assistant_bound_migration_backfill_delete_and_downgrade() -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "alembic"))
    revision = ScriptDirectory.from_config(config).get_revision("0050_assistant_bound")
    assert revision is not None
    assert revision.down_revision == "0049_discovery_metadata"
    module = revision.module
    sql = StringIO()
    with Operations.context(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql}
        )
    ):
        module.upgrade()
        module.downgrade()
    ddl = sql.getvalue().lower()
    # FORCE RLS otherwise silently hides all pre-existing tenant rows from backfill.
    assert ddl.index("no force row level security") < ddl.index("update chat_sessions")
    assert ddl.index("update chat_sessions") < ddl.index("chat_sessions force row level security")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("CREATE TABLE assistants (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE assistant_versions (id INTEGER PRIMARY KEY, "
                "assistant_id INTEGER REFERENCES assistants(id) ON DELETE CASCADE)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE chat_sessions (id INTEGER PRIMARY KEY, "
                "assistant_id INTEGER REFERENCES assistants(id) ON DELETE SET NULL, "
                "assistant_version_id INTEGER REFERENCES assistant_versions(id) "
                "ON DELETE SET NULL)"
            )
            connection.exec_driver_sql("INSERT INTO assistants VALUES (1)")
            connection.exec_driver_sql("INSERT INTO assistant_versions VALUES (2, 1)")
            connection.exec_driver_sql(
                "INSERT INTO chat_sessions VALUES "
                "(1, 1, 2), (2, NULL, 2), (3, 1, NULL), (4, NULL, NULL)"
            )
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            assert connection.exec_driver_sql(
                "SELECT assistant_bound FROM chat_sessions ORDER BY id"
            ).scalars().all() == [True, True, True, False]
            connection.exec_driver_sql("DELETE FROM assistants WHERE id=1")
            assert connection.exec_driver_sql(
                "SELECT assistant_id, assistant_version_id, assistant_bound "
                "FROM chat_sessions ORDER BY id"
            ).all() == [
                (None, None, True),
                (None, None, True),
                (None, None, True),
                (None, None, False),
            ]
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
            assert "assistant_bound" not in {
                c["name"] for c in sa.inspect(connection).get_columns("chat_sessions")
            }
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Answer audit provenance (#581 item 6, INV-6).
# ---------------------------------------------------------------------------


class _RecordingRetrieval(UUIDDocumentResolver):
    def __init__(self) -> None:
        self.source_scopes: list[object] = []

    async def search_text(self, *, principal: object, query: str, k: int, **kwargs: object):  # type: ignore[override]
        self.source_scopes.append(kwargs.get("source_ids"))
        return []

    async def search_documents(  # type: ignore[override]
        self, *, principal: object, name_or_query: str, k: int = 10, **kwargs: object
    ):
        return []

    async def get_document(  # type: ignore[override]
        self, *, principal: object, document_id: uuid.UUID, **kwargs: object
    ):
        return None


class _SearchThenAnswer:
    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None, **kwargs: object
    ) -> AsyncIterator[StreamEvent]:
        msgs = list(messages)  # type: ignore[arg-type]
        has_tool = any(getattr(m, "role", None).value == "tool" for m in msgs)
        if not has_tool and kwargs.get("tool_choice") != "none":
            yield StreamEvent(
                tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "q"}),),
                finish_reason="tool_calls",
            )
        else:
            yield StreamEvent(text="Answer.")
            yield StreamEvent(finish_reason="stop")


async def test_answer_audit_records_assistant_provenance_and_scope(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    session_id = await _published_session(factory, world)
    assistant_id, version_id = uuid.uuid4(), uuid.uuid4()
    config = assemble_run_config(
        {
            "name": "Scoped",
            "instructions": "Scoped.",
            "model": None,
            "knowledgeScope": {
                "collectionIds": [str(world.coll_a)],
                "sourceIds": [str(world.src_1)],
                "modes": ["company", "uploaded"],
            },
            "toolAllowlist": ["search_text"],
            "autonomyLevel": "suggest",
        },
        assistant_id=assistant_id,
        assistant_version_id=version_id,
    )
    retrieval = _RecordingRetrieval()
    runtime = ChatRuntime(
        sessionmaker=factory,
        gateway=_SearchThenAnswer(),  # type: ignore[arg-type]
        backplane=InMemoryBackplane(),
        principal=_principal(world),
        request_id="req-scope",
        source_ip="127.0.0.1",
        default_max_tool_turns=4,
        retrieval_factory=lambda _s: retrieval,  # type: ignore[arg-type,return-value]
    )
    await runtime.run(
        stream_id="scope-audit-1",
        session_id=session_id,
        question="tell me",
        model="test/scoped",
        history=[],
        collection_ids=None,
        assistant_config=config,
    )
    async with factory() as session:
        events = await AuditEventRepository(session, world.tenant).list_recent(limit=50)
    answers = [e for e in events if e.action == "answer.generated"]
    assert retrieval.source_scopes == [[world.src_1]]
    assert answers, "answer.generated audit event missing"
    meta = answers[0].metadata
    assert meta["assistant_id"] == str(assistant_id)
    assert meta["assistant_version_id"] == str(version_id)
    assert meta["effective_modes"] == ["company", "uploaded"]
    assert meta["scope_collection_ids"] == [str(world.coll_a)]
    assert meta["scope_source_ids"] == [str(world.src_1)]


async def test_source_scope_rechecked_before_answer_is_persisted(
    factory: async_sessionmaker[AsyncSession], world: _World
) -> None:
    session_id = await _published_session(factory, world)
    config = assemble_run_config(
        _config(modes=["company"], allowlist=["search_text"], source_ids=[str(world.src_1)])
    )

    class MovingRetrieval(RetrievalService):
        async def search_text(self, **kwargs):
            passages = await super().search_text(**kwargs)
            assert passages
            # Deterministic change between the tool read and the final citation read.
            await self._session.execute(
                sa_update(db_models.Document)
                .where(db_models.Document.id == world.doc_a_s1)
                .values(source_id=world.src_2)
            )
            return passages

    class CitingGateway(_SearchThenAnswer):
        async def stream_tools(self, *args, **kwargs):
            async for event in super().stream_tools(*args, **kwargs):
                if event.text == "Answer.":
                    yield StreamEvent(text="Alpha body. [S1]")
                else:
                    yield event

    store = _SearchStore([SearchHit(world.chunk_a, world.doc_a_s1, 1.0, 0, "b" * 64)])
    runtime = ChatRuntime(
        sessionmaker=factory,
        gateway=CitingGateway(),
        backplane=InMemoryBackplane(),  # type: ignore[arg-type]
        principal=_principal(world),
        request_id="req-scope",
        source_ip="127.0.0.1",
        retrieval_factory=lambda s: MovingRetrieval(s, gateway=_FakeGateway(), store=store),  # type: ignore[arg-type]
    )
    await runtime.run(
        stream_id="scope-recheck",
        session_id=session_id,
        question="tell me",
        model="test/scoped",
        history=[],
        collection_ids=None,
        assistant_config=config,
    )
    async with factory() as session:
        messages = await MessageRepository(session, world.tenant).list_for_session(session_id)
    assert len(messages) == 1
    assert messages[0].content == NO_SOURCES_FALLBACK
