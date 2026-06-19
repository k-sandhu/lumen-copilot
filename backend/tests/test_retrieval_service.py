"""Retrieval adapter tests — the permission chokepoint + the agent tools (issue #45).

Two layers, both honouring the offline-safe pattern of #44:

* **Offline (in-memory SQLite)** — the ``search_documents`` and ``get_document``
  tools issue plain relational SQL (``ilike`` / ordered selects / concatenation),
  which runs on SQLite, so their **permission filter** is fully exercised here
  without Postgres. The headline is **INV-2 (AC-4)**: a tool call as user A never
  returns user B's documents (same tenant) nor another tenant's — it returns
  nothing / "not found", fail-closed.
* **Live (compose Postgres + pgvector)** — the hybrid ``search`` uses
  ``embedding <=> :q`` (pgvector cosine distance) and ``to_tsvector @@
  plainto_tsquery`` (full-text), neither of which exists on SQLite. That path is
  exercised against the real database (schema created with the vector extension),
  asserting hybrid fusion *and* that the permission filter holds end-to-end. It
  skips automatically when Postgres is unreachable.

The query embedding is produced by a **fake** LLM gateway (no network, no key) so
the service is testable offline; the real bge-m3 call is the #36 gateway's own
contract.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest
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
from app.retrieval import RetrievalService

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip

_EMBED_DIM = 1024


class _FakeGateway:
    """A no-network stand-in for the #36 LLM gateway's ``embed``.

    Returns a fixed-dimension vector per input so ``search`` can run without a
    provider key. The *content* of the vector is irrelevant to the offline tool
    tests (they never reach the semantic leg); the live test seeds matching
    vectors directly so similarity is deterministic there too.
    """

    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector if vector is not None else [0.0] * _EMBED_DIM

    async def embed(self, inputs: list[str]) -> list[Embedding]:
        return [Embedding(vector=list(self._vector), model="fake-bge-m3") for _ in inputs]


def _principal(user_id: uuid.UUID, tenant_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(Role.MEMBER,))


# ---------------------------------------------------------------------------
# Offline fixture: in-memory SQLite schema + two tenants, each with one user.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A fresh in-memory SQLite schema + session per test (offline-safe)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


async def _seed_user_with_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    filename: str,
    chunk_texts: list[str],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a user + a collection + a document with chunks; return (user_id, document_id)."""
    user = await UserRepository(session, tenant_id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_id).create(owner_id=user.id, name="C")
    doc = await DocumentRepository(session, tenant_id).create(
        owner_id=user.id,
        collection_id=coll.id,
        filename=filename,
        mime_type="text/plain",
        size_bytes=10,
        storage_key="k",
        status=DocumentStatus.READY,
    )
    offset = 0
    inputs: list[ChunkInput] = []
    for text in chunk_texts:
        inputs.append(ChunkInput(text=text, char_start=offset, char_end=offset + len(text)))
        offset += len(text)
    await ChunkRepository(session, tenant_id).replace_for_document(doc.id, inputs)
    return user.id, doc.id


@pytest_asyncio.fixture
async def two_tenants(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenants = TenantRepository(session)
    a = await tenants.create(name="Tenant A")
    b = await tenants.create(name="Tenant B")
    return a.id, b.id


# ---------------------------------------------------------------------------
# search_documents — permission filter (offline; ilike runs on SQLite).
# ---------------------------------------------------------------------------


async def test_search_documents_returns_own_documents(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_id, doc_id = await _seed_user_with_document(
        session,
        tenant_id=tenant_a,
        email="a@x.test",
        filename="q3-budget.txt",
        chunk_texts=["the quarterly budget figures"],
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    hits = await svc.search_documents(
        principal=_principal(user_id, tenant_a), name_or_query="budget"
    )
    assert [h.document_id for h in hits] == [doc_id]
    assert hits[0].document_name == "q3-budget.txt"


async def test_search_documents_excludes_other_user_same_tenant(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-2 (AC-4): user A's document search never returns user B's docs (same tenant)."""
    tenant_a, _ = two_tenants
    user_a, _doc_a = await _seed_user_with_document(
        session,
        tenant_id=tenant_a,
        email="a@x.test",
        filename="a-budget.txt",
        chunk_texts=["alpha budget"],
    )
    user_b, doc_b = await _seed_user_with_document(
        session,
        tenant_id=tenant_a,
        email="b@x.test",
        filename="b-budget.txt",
        chunk_texts=["beta budget"],
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    # User A searches "budget" — must see only their own, never user B's.
    hits = await svc.search_documents(
        principal=_principal(user_a, tenant_a), name_or_query="budget"
    )
    returned = {h.document_id for h in hits}
    assert doc_b not in returned
    assert returned == {_doc_a}
    # And user B, symmetrically, never sees user A's.
    hits_b = await svc.search_documents(
        principal=_principal(user_b, tenant_a), name_or_query="budget"
    )
    assert {h.document_id for h in hits_b} == {doc_b}


async def test_search_documents_excludes_other_tenant(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1/INV-2: a document in tenant B is invisible to a tenant-A principal."""
    tenant_a, tenant_b = two_tenants
    user_a, _ = await _seed_user_with_document(
        session,
        tenant_id=tenant_a,
        email="a@x.test",
        filename="shared-name.txt",
        chunk_texts=["a"],
    )
    _user_b, doc_b = await _seed_user_with_document(
        session,
        tenant_id=tenant_b,
        email="b@y.test",
        filename="shared-name.txt",
        chunk_texts=["b"],
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    hits = await svc.search_documents(
        principal=_principal(user_a, tenant_a), name_or_query="shared-name"
    )
    assert doc_b not in {h.document_id for h in hits}


async def test_search_documents_blank_query_returns_empty(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a, _ = await _seed_user_with_document(
        session, tenant_id=tenant_a, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    result = await svc.search_documents(principal=_principal(user_a, tenant_a), name_or_query="  ")
    assert result == []


# ---------------------------------------------------------------------------
# get_document — permission filter (offline; plain selects run on SQLite).
# ---------------------------------------------------------------------------


async def test_get_document_returns_reassembled_text(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a, doc_a = await _seed_user_with_document(
        session,
        tenant_id=tenant_a,
        email="a@x.test",
        filename="report.txt",
        chunk_texts=["Hello ", "world."],
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    text = await svc.get_document(principal=_principal(user_a, tenant_a), document_id=doc_a)
    assert text is not None
    assert text.document_name == "report.txt"
    assert text.text == "Hello world."  # chunks concatenated in ord order


async def test_get_document_denies_other_user_same_tenant(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-2 (AC-4): a direct get of user B's document by user A returns None (→ not found)."""
    tenant_a, _ = two_tenants
    user_a, _doc_a = await _seed_user_with_document(
        session, tenant_id=tenant_a, email="a@x.test", filename="a.txt", chunk_texts=["a"]
    )
    _user_b, doc_b = await _seed_user_with_document(
        session, tenant_id=tenant_a, email="b@x.test", filename="b.txt", chunk_texts=["secret b"]
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    # User A directly fetches user B's document id → None (existence non-disclosure).
    assert await svc.get_document(principal=_principal(user_a, tenant_a), document_id=doc_b) is None


async def test_get_document_denies_other_tenant(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1/INV-2: a tenant-A principal cannot fetch a tenant-B document."""
    tenant_a, tenant_b = two_tenants
    user_a, _ = await _seed_user_with_document(
        session, tenant_id=tenant_a, email="a@x.test", filename="a.txt", chunk_texts=["a"]
    )
    _user_b, doc_b = await _seed_user_with_document(
        session, tenant_id=tenant_b, email="b@y.test", filename="b.txt", chunk_texts=["b"]
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    assert await svc.get_document(principal=_principal(user_a, tenant_a), document_id=doc_b) is None


async def test_get_document_missing_id_returns_none(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a, _ = await _seed_user_with_document(
        session, tenant_id=tenant_a, email="a@x.test", filename="a.txt", chunk_texts=["a"]
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    assert (
        await svc.get_document(principal=_principal(user_a, tenant_a), document_id=uuid.uuid4())
        is None
    )


async def test_search_text_empty_query_returns_empty(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A blank query short-circuits before any DB/model call — no passages."""
    tenant_a, _ = two_tenants
    user_a, _ = await _seed_user_with_document(
        session, tenant_id=tenant_a, email="a@x.test", filename="a.txt", chunk_texts=["a"]
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    assert await svc.search_text(principal=_principal(user_a, tenant_a), query="   ", k=5) == []


# ---------------------------------------------------------------------------
# Live hybrid search against compose Postgres + pgvector. Skipped if offline.
# ---------------------------------------------------------------------------

_PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen"
)


def _pg_reachable(url: str) -> bool:
    parsed = urlparse(url.replace("postgresql+asyncpg", "postgresql"))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_live = pytest.mark.skipif(
    not _pg_reachable(_PG_URL),
    reason=(
        f"Postgres not reachable for {_PG_URL}; live hybrid-retrieval test skipped "
        "(offline-safe). The real pgvector + full-text path runs only here."
    ),
)


def _unit_vector(dim: int, hot: int) -> list[float]:
    """A unit-ish vector with a single hot dimension — gives deterministic cosine order."""
    v = [0.0] * dim
    v[hot % dim] = 1.0
    return v


@_live
async def test_live_hybrid_search_is_permission_filtered() -> None:
    """End-to-end on real Postgres: hybrid fusion AND the INV-2 permission filter.

    Seeds two users in two tenants, each owning a document whose chunk text and
    embedding match the query. User A's hybrid ``search`` must return only user
    A's passage — never user B's, even though both match the query text/vector.
    Proves the pgvector + full-text legs and the permission filter together.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine as _create

    engine = _create(_PG_URL)
    schema = f"ret_test_{uuid.uuid4().hex[:8]}"
    try:
        # Isolated schema so the live test never collides with app data.
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            # Include `public` so pgvector's `vector` type (the extension lives in
            # public) resolves while new tables are created in the isolated schema.
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            await sess.execute(sql_text(f'SET search_path TO "{schema}", public'))

            tenant_a = (await TenantRepository(sess).create(name="A")).id
            tenant_b = (await TenantRepository(sess).create(name="B")).id

            hot = 7
            query_text = "annual revenue growth strategy"
            # User A: matching chunk text + matching embedding.
            user_a = await UserRepository(sess, tenant_a).create(
                email="a@x.test", password_hash="h", roles=[Role.MEMBER]
            )
            coll_a = await CollectionRepository(sess, tenant_a).create(owner_id=user_a.id, name="C")
            doc_a = await DocumentRepository(sess, tenant_a).create(
                owner_id=user_a.id,
                collection_id=coll_a.id,
                filename="a.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key="k",
                status=DocumentStatus.READY,
            )
            await ChunkRepository(sess, tenant_a).replace_for_document(
                doc_a.id,
                [
                    ChunkInput(
                        text="annual revenue growth strategy for the year",
                        char_start=0,
                        char_end=43,
                        embedding=_unit_vector(_EMBED_DIM, hot),
                    )
                ],
            )

            # User B (other tenant): same matching text + embedding.
            user_b = await UserRepository(sess, tenant_b).create(
                email="b@y.test", password_hash="h", roles=[Role.MEMBER]
            )
            coll_b = await CollectionRepository(sess, tenant_b).create(owner_id=user_b.id, name="C")
            doc_b = await DocumentRepository(sess, tenant_b).create(
                owner_id=user_b.id,
                collection_id=coll_b.id,
                filename="b.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key="k",
                status=DocumentStatus.READY,
            )
            await ChunkRepository(sess, tenant_b).replace_for_document(
                doc_b.id,
                [
                    ChunkInput(
                        text="annual revenue growth strategy for the year",
                        char_start=0,
                        char_end=43,
                        embedding=_unit_vector(_EMBED_DIM, hot),
                    )
                ],
            )
            await sess.commit()

            gateway = _FakeGateway(_unit_vector(_EMBED_DIM, hot))
            svc = RetrievalService(sess, gateway=gateway)

            # User A's hybrid search returns their passage with source + offsets.
            passages = await svc.search(
                principal=_principal(user_a.id, tenant_a), query=query_text, k=5
            )
            assert [p.document_id for p in passages] == [doc_a.id]
            assert passages[0].document_name == "a.txt"
            assert passages[0].char_start == 0 and passages[0].char_end == 43
            assert passages[0].score > 0

            # INV-2 end-to-end: user A never sees user B's matching passage.
            returned_docs = {p.document_id for p in passages}
            assert doc_b.id not in returned_docs
    finally:
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
