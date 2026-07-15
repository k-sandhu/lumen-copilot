"""Index-sync write-path tests (ADR-0010 §5, epic #189 slice 2 / #191).

Offline throughout (in-memory SQLite + a fake store): the engine round-trip
itself is proven live in ``test_search_store.py``; these tests prove the
**wiring** — that every mutation path converges the index on Postgres:

* the sync core replaces (delete → upsert) a live document's chunk docs, and
  deletes when the document is gone or empty — idempotent by construction;
* ingestion dual-writes (a ``ready`` document is indexed) and an engine fault
  fails the run as a retryable :class:`IngestionError` (retry as a unit);
* the request-path deletion hooks (documents / collections services) enqueue
  the sync **after commit** through the single ``tasks.enqueue_index_sync``
  seam;
* the backfill (``reindex_tenant``) sweeps exactly one tenant's documents
  through the same core (idempotent, keyset-resumable).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from typing import ClassVar

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.core.config import Settings
from app.core.errors import DependencyError, NotFoundError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import Embedding
from app.search import IndexedChunk
from app.search.reindex import reindex_tenant
from app.services.audit import AuditSink
from app.services.collections_service import CollectionsService
from app.services.document_service import DocumentService
from app.tasks.index_sync import sync_document_index_async
from app.tasks.ingest import IngestionError, ingest_document_async

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip

_DIM = 8


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "k",
        "S3_SECRET_KEY": "s",
        "S3_BUCKET": "b",
        "OPENROUTER_API_KEY": "",
        "LLM_EMBEDDING_DIMENSIONS": str(_DIM),
        **overrides,
    }
    return Settings(**base)  # type: ignore[arg-type]


# --- Fakes -------------------------------------------------------------------


class _FakeIndexStore:
    """Records the store calls the write path makes; injectable failure."""

    instances: ClassVar[list[_FakeIndexStore]] = []

    def __init__(self) -> None:
        self.ensured = 0
        self.upserts: list[list[IndexedChunk]] = []
        self.deletes: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.closed = False
        self.fail_upsert: Exception | None = None
        type(self).instances.append(self)

    @classmethod
    def from_settings(cls, settings: Settings) -> _FakeIndexStore:
        return cls()

    async def ensure_index(self) -> None:
        self.ensured += 1

    async def upsert_chunks(
        self, chunks: Sequence[IndexedChunk], *, refresh: bool = False
    ) -> None:
        if self.fail_upsert is not None:
            raise self.fail_upsert
        self.upserts.append(list(chunks))

    async def delete_document(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID, refresh: bool = False
    ) -> None:
        self.deletes.append((tenant_id, document_id))

    async def aclose(self) -> None:
        self.closed = True


class _FakeObjectStore:
    """Minimal object store: get() returns seeded bytes, delete() records."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []

    def put(self, tenant_id: str, key: str, data: bytes) -> None:
        self._objects[(tenant_id, key)] = data

    async def get(self, tenant_id: str, key: str) -> bytes:
        try:
            return self._objects[(tenant_id, key)]
        except KeyError as exc:
            raise NotFoundError("object not found", code="object_not_found") from exc

    async def delete(self, tenant_id: str, key: str) -> None:
        self.deleted.append((tenant_id, key))


class _FakeGateway:
    """Deterministic embeddings, no network."""

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        return [Embedding(vector=[1.0] * _DIM, model="fake") for _ in inputs]


# --- SQLite-backed session_scope override (the ingestion-task test pattern) --


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    prev_engine = db_session._engine
    prev_maker = db_session._sessionmaker
    db_session._engine = engine
    db_session._sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield
    finally:
        db_session._engine = prev_engine
        db_session._sessionmaker = prev_maker
        await engine.dispose()


async def _seed(
    *,
    chunk_texts: list[str],
    with_embedding: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Tenant + user + collection + doc (+chunks); returns their ids."""
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        user = await UserRepository(session, tenant.id).create(
            email=f"{uuid.uuid4().hex[:8]}@acme.test", password_hash="h", roles=[Role.MEMBER]
        )
        coll = await CollectionRepository(session, tenant.id).create(owner_id=user.id, name="c")
        doc = await DocumentRepository(session, tenant.id).create(
            owner_id=user.id,
            collection_id=coll.id,
            filename="f.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key="k",
            status=DocumentStatus.READY,
        )
        offset = 0
        inputs: list[ChunkInput] = []
        for text in chunk_texts:
            inputs.append(
                ChunkInput(
                    text=text,
                    char_start=offset,
                    char_end=offset + len(text),
                    embedding=[0.5] * _DIM if with_embedding else None,
                )
            )
            offset += len(text)
        await ChunkRepository(session, tenant.id).replace_for_document(doc.id, inputs)
    return tenant.id, user.id, coll.id, doc.id


# --- The sync core: converge the index on Postgres ----------------------------


async def test_sync_replaces_live_document_chunks(sqlite_engine: None) -> None:
    """Live doc: delete-then-upsert with owner/collection/vector/span intact."""
    tenant_id, user_id, coll_id, doc_id = await _seed(chunk_texts=["alpha", "beta"])
    store = _FakeIndexStore()

    result = await sync_document_index_async(
        tenant_id, doc_id, settings=_settings(), store=store
    )

    assert result.indexed_count == 2 and result.deleted is False
    assert store.ensured == 1
    assert store.deletes == [(tenant_id, doc_id)]  # stale ids cleared first
    [batch] = store.upserts
    assert [c.text for c in batch] == ["alpha", "beta"]
    first = batch[0]
    assert first.tenant_id == tenant_id
    assert first.owner_id == user_id
    assert first.collection_id == coll_id
    assert first.document_id == doc_id
    assert first.embedding == tuple([0.5] * _DIM)
    assert (first.char_start, first.char_end) == (0, 5)
    assert store.closed is False  # injected store is caller-owned


async def test_sync_missing_document_deletes_only(sqlite_engine: None) -> None:
    tenant_id, _, _, _ = await _seed(chunk_texts=["x"])
    ghost = uuid.uuid4()
    store = _FakeIndexStore()

    result = await sync_document_index_async(
        tenant_id, ghost, settings=_settings(), store=store
    )

    assert result.deleted is True and result.indexed_count == 0
    assert store.deletes == [(tenant_id, ghost)]
    assert store.upserts == []


async def test_sync_chunkless_document_deletes_only(sqlite_engine: None) -> None:
    tenant_id, _, _, doc_id = await _seed(chunk_texts=[])
    store = _FakeIndexStore()

    result = await sync_document_index_async(
        tenant_id, doc_id, settings=_settings(), store=store
    )

    assert result.deleted is True
    assert store.deletes == [(tenant_id, doc_id)]
    assert store.upserts == []


# --- Ingestion dual-write (ADR-0010 §5) ---------------------------------------


async def test_ingest_dual_writes_to_the_index(sqlite_engine: None) -> None:
    """A document that reports `ready` is indexed — dual-write on the ingest path."""
    tenant_id, _, _, doc_id = await _seed(chunk_texts=[])
    objects = _FakeObjectStore()
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(doc_id)
        assert doc is not None
        objects.put(str(tenant_id), doc.storage_key, b"hello indexed world " * 20)
    store = _FakeIndexStore()

    result = await ingest_document_async(
        tenant_id,
        doc_id,
        settings=_settings(INGESTION_CHUNK_SIZE="120", INGESTION_CHUNK_OVERLAP="20"),
        object_store=objects,  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
        search_store=store,  # type: ignore[arg-type]
    )

    assert result.status is DocumentStatus.READY and result.chunk_count > 0
    [batch] = store.upserts
    assert len(batch) == result.chunk_count
    assert all(c.embedding == tuple([1.0] * _DIM) for c in batch)


async def test_ingest_index_failure_is_retryable(sqlite_engine: None) -> None:
    """Engine down during ingest → IngestionError (Celery retries the unit)."""
    tenant_id, _, _, doc_id = await _seed(chunk_texts=[])
    objects = _FakeObjectStore()
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(doc_id)
        assert doc is not None
        objects.put(str(tenant_id), doc.storage_key, b"some text to chunk " * 20)
    store = _FakeIndexStore()
    store.fail_upsert = DependencyError("engine down", code="search_unavailable")

    with pytest.raises(IngestionError):
        await ingest_document_async(
            tenant_id,
            doc_id,
            settings=_settings(INGESTION_CHUNK_SIZE="120", INGESTION_CHUNK_OVERLAP="20"),
            object_store=objects,  # type: ignore[arg-type]
            gateway=_FakeGateway(),  # type: ignore[arg-type]
            search_store=store,  # type: ignore[arg-type]
        )


# --- Request-path deletion hooks (after-commit, via the single enqueue seam) --


async def test_document_delete_enqueues_index_sync_after_commit(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, user_id, _, doc_id = await _seed(chunk_texts=["x"])
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_index_sync", lambda t, d: calls.append((t, d))
    )

    async with db_session.session_scope() as session:
        svc = DocumentService(
            session,
            tenant_id=tenant_id,
            owner_id=user_id,
            object_store=_FakeObjectStore(),  # type: ignore[arg-type]
            audit=AuditSink(AuditEventRepository(session, tenant_id)),
            request_id="r",
            source_ip="i",
            upload_allowed_content_types=frozenset({"text/plain"}),
            max_upload_bytes=1024,
        )
        assert await svc.delete(doc_id) is True
        assert calls == []  # not before commit
    # session_scope commits on exit → the one-shot after_commit listener fired.
    assert calls == [(tenant_id, doc_id)]


async def test_collection_delete_enqueues_index_sync_per_document(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, user_id, coll_id, doc_id = await _seed(chunk_texts=["x"])
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_index_sync", lambda t, d: calls.append((t, d))
    )

    async with db_session.session_scope() as session:
        svc = CollectionsService(
            session,
            tenant_id=tenant_id,
            owner_id=user_id,
            object_store=_FakeObjectStore(),  # type: ignore[arg-type]
            audit=AuditSink(AuditEventRepository(session, tenant_id)),
            request_id="r",
            source_ip="i",
        )
        assert await svc.delete(coll_id) is True
        assert calls == []
    assert calls == [(tenant_id, doc_id)]  # one sync per cascaded document


# --- Backfill ------------------------------------------------------------------


async def test_reindex_tenant_sweeps_only_that_tenant(sqlite_engine: None) -> None:
    """The backfill syncs every document of the tenant — and no one else's."""
    tenant_a, _, _, doc_a1 = await _seed(chunk_texts=["a one"])
    async with db_session.session_scope() as session:
        # A second document in tenant A, plus a whole other tenant B.
        docs_a = DocumentRepository(session, tenant_a)
        first = await docs_a.get(doc_a1)
        assert first is not None
        doc_a2 = await docs_a.create(
            owner_id=first.owner_id,
            collection_id=first.collection_id,
            filename="g.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key="k2",
            status=DocumentStatus.READY,
        )
    tenant_b, _, _, doc_b = await _seed(chunk_texts=["b one"])
    store = _FakeIndexStore()

    synced = await reindex_tenant(tenant_a, store=store, page_size=1)  # exercise keyset

    assert synced == 2
    synced_ids = {d for _, d in store.deletes}
    assert synced_ids == {doc_a1, doc_a2.id}
    assert doc_b not in synced_ids  # tenant B untouched (INV-1 enumeration)


async def test_list_ids_page_is_keyset_resumable(sqlite_engine: None) -> None:
    tenant_id, _, _, _ = await _seed(chunk_texts=["x"])
    async with db_session.session_scope() as session:
        docs = DocumentRepository(session, tenant_id)
        existing = await docs.list_ids_page(after_id=None, limit=10)
        page1 = await docs.list_ids_page(after_id=None, limit=1)
        page2 = await docs.list_ids_page(after_id=page1[-1], limit=10)
    assert page1[0] == existing[0]
    assert page1[0] not in page2
    assert set(page1) | set(page2) == set(existing)
