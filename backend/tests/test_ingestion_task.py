"""Ingestion task tests — the async pipeline core (CC-5 #21, AC-1..AC-6).

Exercises :func:`app.tasks.ingest.ingest_document_async` end-to-end against an
in-memory SQLite database (the portable column types make ``chunks`` work without
Postgres), a **fake object store**, and a **fake LLM gateway** so the suite is
offline (no MinIO, no model key — AC: embedding-dependent tests fake ``embed()``).

Coverage:

* AC-1..AC-3: a stored text document is fetched, parsed, chunked, embedded
  (batched), and chunks persist with vector + offsets + tenant + document + ord;
* AC-5: status advances ``pending → processing → ready`` with ``chunk_count``;
  a **re-run replaces** the chunks (idempotent, no duplicates);
* AC-6 (negative): a corrupt/unsupported document ends ``status=failed`` with a
  recorded reason (not a crash, not a silent drop);
* embeddings are **batched** (the fake counts its calls);
* INV-1: chunks persist under the document's tenant only;
* transient faults (storage/model unavailable) raise the retryable error.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.core.config import Settings
from app.core.errors import DependencyError, NotFoundError
from app.db.base import Base
from app.db.repositories import (
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import Embedding
from app.tasks.ingest import IngestionError, ingest_document_async

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip

_DIM = 8  # tiny embedding width for the tests (gateway is faked)


# --- Test settings ----------------------------------------------------------


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
        "INGESTION_CHUNK_SIZE": "120",
        "INGESTION_CHUNK_OVERLAP": "20",
        "INGESTION_EMBED_BATCH_SIZE": "3",
        "LLM_EMBEDDING_DIMENSIONS": str(_DIM),
        **overrides,
    }
    return Settings(**base)  # type: ignore[arg-type]


# --- Fakes ------------------------------------------------------------------


class _FakeObjectStore:
    """In-memory object store keyed like the real adapter (tenant-prefixed)."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self.fail_with: Exception | None = None

    def put(self, tenant_id: str, key: str, data: bytes) -> None:
        self._objects[(tenant_id, key)] = data

    async def get(self, tenant_id: str, key: str) -> bytes:
        if self.fail_with is not None:
            raise self.fail_with
        try:
            return self._objects[(tenant_id, key)]
        except KeyError as exc:
            raise NotFoundError("object not found", code="object_not_found") from exc


class _FakeGateway:
    """Fake LLM gateway: returns a deterministic vector per input, counts batches."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[int] = []  # batch sizes, in order

    async def embed(self, inputs: Sequence[str], *, model: str | None = None) -> list[Embedding]:
        if self.fail:
            raise DependencyError("no key", code="llm_unconfigured")
        self.calls.append(len(inputs))
        return [Embedding(vector=[float(len(t) % 7)] * _DIM, model="fake") for t in inputs]


# --- SQLite-backed session_scope override -----------------------------------


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[None]:
    """Point ``db.session`` globals at a fresh in-memory SQLite for the task.

    The task calls ``session_scope()`` (which uses the module-global sessionmaker),
    so we install a SQLite-backed engine/sessionmaker on the module for the test
    and restore the originals afterwards. ``StaticPool`` keeps the single
    in-memory connection alive across ``session_scope`` checkouts.
    """
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


async def _seed_document(*, mime_type: str, key: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Create tenant/user/collection/document (status=pending); return ids."""
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        user = await UserRepository(session, tenant.id).create(
            email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
        )
        coll = await CollectionRepository(session, tenant.id).create(owner_id=user.id, name="c")
        doc = await DocumentRepository(session, tenant.id).create(
            owner_id=user.id,
            collection_id=coll.id,
            filename="f",
            mime_type=mime_type,
            size_bytes=1,
            storage_key=key,
        )
    return tenant.id, doc.id


# --- Happy path (AC-1..AC-3, AC-5) ------------------------------------------


async def test_ingest_text_document_persists_chunks_and_marks_ready(
    sqlite_engine: None,
) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    body = (
        "The quick brown fox jumps over the lazy dog. " * 10
        + "Pack my box with five dozen liquor jugs. " * 10
    ).encode("utf-8")
    key = "k-text"
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="placeholder")
    # Store under the real storage_key the seeded document carries.
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, body)

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )

    assert result.status is DocumentStatus.READY
    assert result.chunk_count > 1

    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        assert doc.status is DocumentStatus.READY
        assert doc.error is None

        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        assert len(chunks) == result.chunk_count
        # AC-2/AC-3: each chunk has offsets, an embedding of the right width, the
        # tenant + document scope, and contiguous ordinals.
        text = body.decode()
        for i, c in enumerate(chunks):
            assert c.ord == i
            assert c.tenant_id == tenant_id
            assert c.document_id == document_id
            assert c.embedding is not None and len(c.embedding) == _DIM
            assert text[c.char_start : c.char_end] == c.text


async def test_embeddings_are_batched(sqlite_engine: None) -> None:
    """The gateway is called once per batch, not once per chunk (batched)."""
    settings = _settings(INGESTION_EMBED_BATCH_SIZE="3")
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="x")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, ("sentence number x. " * 60).encode())

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )
    n = result.chunk_count
    assert n > 3  # enough chunks that batching is observable
    # Number of embed() calls == ceil(n / batch_size), each <= batch_size.
    assert len(gateway.calls) == (n + 2) // 3
    assert all(size <= 3 for size in gateway.calls)
    assert sum(gateway.calls) == n


async def test_reingest_replaces_chunks_idempotent(sqlite_engine: None) -> None:
    """AC-5: re-running ingestion replaces, never duplicates, a document's chunks."""
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="y")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, ("alpha beta gamma delta. " * 40).encode())

    first = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    second = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )

    assert first.chunk_count == second.chunk_count
    async with db_session.session_scope() as session:
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        # Replaced, not duplicated.
        assert len(chunks) == second.chunk_count
        # Ordinals still contiguous after the replace.
        assert [c.ord for c in chunks] == list(range(len(chunks)))


# --- Negatives (AC-6) -------------------------------------------------------


async def test_unsupported_type_marks_failed(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="image/png", key="z")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, b"\x89PNG not an ingestible type")

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.FAILED
    assert result.error  # a recorded reason, not None

    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        assert doc.status is DocumentStatus.FAILED
        assert doc.error


async def test_corrupt_pdf_marks_failed_not_crash(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="application/pdf", key="p")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, b"%PDF-1.4 broken")

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.FAILED
    assert "PDF" in (result.error or "")


async def test_empty_document_is_ready_with_zero_chunks(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="e")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, b"   \n  ")  # whitespace only

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.READY
    assert result.chunk_count == 0
    # And no chunks were left behind / created.
    async with db_session.session_scope() as session:
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        assert chunks == []


async def test_missing_document_is_noop(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.FAILED
    assert "not found" in (result.error or "")


# --- Transient faults are retryable (raised, not swallowed) -----------------


async def test_storage_unavailable_raises_retryable(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    store.fail_with = DependencyError("minio down", code="dependency_unavailable")
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="s")
    with pytest.raises(IngestionError):
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=settings,
            object_store=store,
            gateway=gateway,  # type: ignore[arg-type]
        )


async def test_embed_unavailable_raises_retryable(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway(fail=True)

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="m")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, ("text to embed. " * 30).encode())

    with pytest.raises(IngestionError):
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=settings,
            object_store=store,
            gateway=gateway,  # type: ignore[arg-type]
        )
