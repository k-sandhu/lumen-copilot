"""Connector sync task tests — fetch + ingest, fully mocked (#20, ADR-0009 §5).

Exercises :func:`app.tasks.sync_source.sync_source_async` end-to-end **offline**:
the network fetch is mocked (the connector's ``sync`` is monkeypatched to return
``FetchedDoc``s — no socket), the object store + LLM gateway are fakes (no MinIO,
no model key), and the DB is in-memory SQLite. So the test asserts the seam — a
source's fetched docs flow through the **reused ingestion pipeline** into chunks
linked to ``source_id`` — without any live dependency.

Coverage:

* happy path: fetched docs → Documents (``source_id`` set) → chunks; the source
  advances to ``ready`` with ``indexed_count`` + ``last_synced_at``;
* idempotent re-sync: a second run **replaces** the prior docs (no duplicates);
* a fetch/SSRF fault marks the source ``error`` with ``last_error`` (no crash);
* INV-1: the ingested documents + chunks are scoped to the source's tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from importlib import import_module
from typing import ClassVar

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.connectors.base import FetchedDoc
from app.connectors.web.fetch import UrlBlockedError
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import DocumentStatus, Role, Source, SourceStatus
from app.domain.llm import Embedding
from app.services.audit import AuditSink
from app.services.sources_service import SourcesService
from app.tasks.sync_source import SyncResult, sync_source_async
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_context

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_DIM = 8
sync_source_module = import_module("app.tasks.sync_source")


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


class _FakeObjectStore:
    """In-memory store mirroring the real ``ObjectStore.put``/``get`` surface."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, *, tenant_id: str, data: bytes, content_type: str, filename: str):  # noqa: ANN201
        from app.storage.keys import build_key
        from app.storage.object_store import StoredObject

        key = build_key(tenant_id, data, filename)
        self.objects[key] = data
        return StoredObject(
            key=key,
            sha256=key.split("/")[1],
            size_bytes=len(data),
            content_type=content_type,
        )

    async def get(self, tenant_id: str, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, tenant_id: str, key: str) -> None:
        # Idempotent, like the real MinIO adapter (a missing key is a no-op).
        self.objects.pop(key, None)


class _FakeGateway:
    """Fake LLM gateway: a deterministic vector per input (offline)."""

    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        return [Embedding(vector=[float(len(t) % 5)] * _DIM, model="fake") for t in inputs]


class _FakeIndexStore:
    """Offline stand-in for ``app.search.OpenSearchStore`` in the index-sync core.

    The ingest/re-sync paths dual-write to the search index (ADR-0010 §5); this
    keeps that write offline and records the ordered (op, document_id) events so
    the stale-cleanup behaviour is assertable.
    """

    events: ClassVar[list[tuple[str, uuid.UUID]]] = []

    @classmethod
    def from_settings(cls, settings: object) -> _FakeIndexStore:
        return cls()

    async def ensure_index(self) -> None:
        return None

    async def upsert_chunks(self, chunks: Sequence[object], *, refresh: bool = False) -> None:
        for chunk in chunks:
            type(self).events.append(("upsert", chunk.document_id))  # type: ignore[attr-defined]

    async def delete_document(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID, refresh: bool = False
    ) -> None:
        type(self).events.append(("delete", document_id))

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _offline_index_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the index-sync core at the fake store — no engine, tests stay offline."""
    _FakeIndexStore.events = []
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", _FakeIndexStore)


@pytest.fixture(autouse=True)
def _no_broker_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the after-commit enqueue inert AND inline (#271 review).

    This suite drives the sync task directly; the SourcesService commits it
    performs must not leak real Redis/broker work onto executor threads that
    outlive the test (offline contract + fixture-teardown isolation).
    """
    monkeypatch.setattr(
        "app.services.sources_service._dispatch_off_loop",
        lambda fn, *, name: fn(),
    )
    monkeypatch.setattr("app.tasks.enqueue_source_sync", lambda *a, **k: None)
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *a, **k: None)


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[None]:
    """Point ``db.session`` globals at a fresh in-memory SQLite for the task."""
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
    db_session._sessionmaker = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    try:
        yield
    finally:
        db_session._engine = prev_engine
        db_session._sessionmaker = prev_maker
        await engine.dispose()


async def _seed_source(url: str = "http://93.184.216.34/page") -> tuple[uuid.UUID, uuid.UUID]:
    """Create a tenant/user and add a web source via the service; return ids."""
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        user = await UserRepository(session, tenant.id).create(
            email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
        )
        svc = SourcesService(
            session,
            tenant_id=tenant.id,
            owner_id=user.id,
            object_store=_FakeObjectStore(),  # type: ignore[arg-type]
            audit=AuditSink(AuditEventRepository(session, tenant.id)),
            denials=denial_context(
                RecordingDurableAuditTransactions(), session, tenant.id, user.id
            ),
            request_id="r",
            source_ip="203.0.113.1",
        )
        # The after-commit enqueue is a no-op here (no broker / not committed in
        # this scope path); we drive the sync task directly below.
        source = await svc.add(source_type="web", url=url)
        await session.commit()
    return tenant.id, source.id


def _patch_sync(monkeypatch: pytest.MonkeyPatch, docs: list[FetchedDoc] | Exception) -> None:
    """Replace the web connector's ``sync`` so no network is touched."""

    async def _fake_sync(self: object, source: Source, run: object) -> Iterable[FetchedDoc]:
        if isinstance(docs, Exception):
            raise docs
        return list(docs)

    monkeypatch.setattr("app.connectors.web.connector.WebConnector.sync", _fake_sync)


async def _run(tenant_id: uuid.UUID, source_id: uuid.UUID) -> SyncResult:
    return await sync_source_async(
        tenant_id,
        source_id,
        settings=_settings(),
        object_store=_FakeObjectStore(),  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
    )


# --- happy path -------------------------------------------------------------


async def test_sync_ingests_fetched_docs_into_chunks(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, source_id = await _seed_source()
    body = "The quick brown fox jumps over the lazy dog. " * 8
    _patch_sync(
        monkeypatch,
        [
            FetchedDoc(title="Page One", text=body, url="http://93.184.216.34/a"),
            FetchedDoc(title="Page Two", text=body, url="http://93.184.216.34/b"),
        ],
    )

    await _run(tenant_id, source_id)

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.status is SourceStatus.READY
        assert source.indexed_count == 2
        assert source.last_synced_at is not None
        assert source.last_error is None

        docs = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert len(docs) == 2
        # INV-1: documents + chunks carry the source's tenant and link back.
        for doc in docs:
            assert doc.tenant_id == tenant_id
            chunks = await ChunkRepository(session, tenant_id).list_for_document(doc.id)
            assert chunks  # the reused ingestion pipeline produced chunks
            assert all(c.embedding is not None and len(c.embedding) == _DIM for c in chunks)


async def test_sync_refines_mode_from_fanout(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The creation-time URL heuristic mode is **refined** during sync (ADR-0009 §2).

    The seed URL (``.../page``) gets ``mode=page`` at creation; a multi-doc sync
    refines it to ``feed`` (a fan-out). Regression guard for the review finding
    that ``mode`` is populated at creation and refined during sync.
    """
    tenant_id, source_id = await _seed_source(url="http://93.184.216.34/page")
    # Confirm the creation-time heuristic seeded a non-null mode.
    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None and source.config.get("mode") == "page"

    body = "Sphinx of black quartz, judge my vow. " * 8
    _patch_sync(
        monkeypatch,
        [
            FetchedDoc(title="Item A", text=body, url="http://93.184.216.34/a"),
            FetchedDoc(title="Item B", text=body, url="http://93.184.216.34/b"),
        ],
    )
    await _run(tenant_id, source_id)

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.config.get("mode") == "feed"  # refined from the fan-out


async def test_sync_preserves_sitemap_mode_for_xml_url(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.xml`` source seeded ``sitemap`` keeps ``sitemap`` after a multi-doc sync."""
    tenant_id, source_id = await _seed_source(url="http://93.184.216.34/sitemap.xml")
    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None and source.config.get("mode") == "sitemap"

    body = "Lorem ipsum dolor sit amet. " * 8
    _patch_sync(
        monkeypatch,
        [
            FetchedDoc(title="P1", text=body, url="http://93.184.216.34/p1"),
            FetchedDoc(title="P2", text=body, url="http://93.184.216.34/p2"),
        ],
    )
    await _run(tenant_id, source_id)

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.config.get("mode") == "sitemap"  # sitemap preserved, not feed


async def test_resync_replaces_prior_docs(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, source_id = await _seed_source()
    body = "Pack my box with five dozen liquor jugs. " * 8

    _patch_sync(
        monkeypatch,
        [FetchedDoc(title="Old", text=body, url="http://93.184.216.34/old")],
    )
    await _run(tenant_id, source_id)
    async with db_session.session_scope() as session:
        [old_doc] = await DocumentRepository(session, tenant_id).list_for_source(source_id)

    # Second sync returns a different single doc — must replace, not accumulate.
    _patch_sync(
        monkeypatch,
        [FetchedDoc(title="New", text=body, url="http://93.184.216.34/new")],
    )
    await _run(tenant_id, source_id)

    async with db_session.session_scope() as session:
        docs = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert len(docs) == 1
        assert docs[0].filename.endswith("New.txt")
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None and source.indexed_count == 1

    # ADR-0010 §5: the replaced document's chunk docs were cleaned from the
    # search index — its LAST recorded index event is a delete with no upsert
    # after it (the stale-cleanup pass runs after the reconcile transaction).
    old_events = [op for op, doc_id in _FakeIndexStore.events if doc_id == old_doc.id]
    assert old_events and old_events[-1] == "delete"


async def test_resync_deletes_prior_objects_no_orphans(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-sync removes the PRIOR documents' stored objects, not just their rows
    — otherwise every re-sync leaks the previous object set into the bucket (#269).
    Uses ONE shared object store across both syncs so the orphan is observable."""
    tenant_id, source_id = await _seed_source()
    store = _FakeObjectStore()
    body = "Pack my box with five dozen liquor jugs. " * 8

    async def run() -> None:
        await sync_source_async(
            tenant_id,
            source_id,
            settings=_settings(),
            object_store=store,  # type: ignore[arg-type]
            gateway=_FakeGateway(),  # type: ignore[arg-type]
        )

    _patch_sync(monkeypatch, [FetchedDoc(title="Old", text=body, url="http://93.184.216.34/old")])
    await run()
    keys_after_first = set(store.objects)
    assert len(keys_after_first) == 1  # the "Old" object is stored

    # Re-sync with a differently-titled doc → a NEW content-addressed key. The
    # prior "Old" object must be removed, leaving exactly the "New" object.
    _patch_sync(monkeypatch, [FetchedDoc(title="New", text=body, url="http://93.184.216.34/new")])
    await run()

    keys_after_resync = set(store.objects)
    assert len(keys_after_resync) == 1, "the prior object must be deleted, not orphaned"
    assert keys_after_resync.isdisjoint(keys_after_first), "the old key is gone, the new remains"


# --- failure path -----------------------------------------------------------


async def test_fetch_block_marks_source_error(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, source_id = await _seed_source()
    _patch_sync(monkeypatch, UrlBlockedError("redirect to 127.0.0.1 refused"))

    await _run(tenant_id, source_id)

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.status is SourceStatus.ERROR
        assert source.last_error is not None
        assert "fetch failed" in source.last_error
        # No documents were ingested.
        docs = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert docs == []


async def test_sync_all_docs_failing_marks_source_error(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, source_id = await _seed_source()
    body = "The five boxing wizards jump quickly. " * 8
    _patch_sync(
        monkeypatch,
        [
            FetchedDoc(title="Page One", text=body, url="http://93.184.216.34/a"),
            FetchedDoc(title="Page Two", text=body, url="http://93.184.216.34/b"),
        ],
    )

    async def _fail_ingest(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(sync_source_module, "_ingest_one", _fail_ingest)

    result = await _run(tenant_id, source_id)

    assert result.status is SourceStatus.ERROR
    assert result.indexed_count == 0
    assert result.error is not None
    assert "failed" in result.error
    assert "ingest" in result.error

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.status is SourceStatus.ERROR
        assert source.last_error is not None
        assert "failed" in source.last_error
        assert "ingest" in source.last_error
        assert source.indexed_count == 0
        assert source.last_synced_at is None

        docs = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert docs == []


async def test_resync_all_docs_failing_zeroes_stale_indexed_count(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-sync that fails every doc must reset a prior non-zero ``indexed_count``.

    Regression for #157: the failed-sync path first deletes the source's prior
    documents (Phase 3), then routes to ``_fail``. If ``_fail`` does not write
    ``indexed_count=0``, ``update_status`` leaves the stale count from the earlier
    successful sync — leaving the source ``error`` with e.g. ``indexed_count == 2``
    while its document list is empty. Unlike the never-synced case, this starts
    from a source that already indexed >0.
    """
    tenant_id, source_id = await _seed_source()
    body = "The five boxing wizards jump quickly. " * 8

    # First sync succeeds → indexed_count > 0.
    _patch_sync(
        monkeypatch,
        [
            FetchedDoc(title="Page One", text=body, url="http://93.184.216.34/a"),
            FetchedDoc(title="Page Two", text=body, url="http://93.184.216.34/b"),
        ],
    )
    await _run(tenant_id, source_id)

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.status is SourceStatus.READY
        assert source.indexed_count == 2

    # Re-sync: docs are still fetched, but every ingest fails → all-failed guard.
    async def _fail_ingest(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(sync_source_module, "_ingest_one", _fail_ingest)

    result = await _run(tenant_id, source_id)

    assert result.status is SourceStatus.ERROR
    assert result.indexed_count == 0

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.status is SourceStatus.ERROR
        # The stale count from the first (successful) sync must be cleared to 0.
        assert source.indexed_count == 0
        # And the doc list is empty (the re-sync deleted the prior docs).
        docs = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert docs == []


async def test_sync_empty_fetch_stays_ready(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, source_id = await _seed_source()
    _patch_sync(monkeypatch, [])

    result = await _run(tenant_id, source_id)

    assert result.status is SourceStatus.READY
    assert result.indexed_count == 0
    assert result.error is None

    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        assert source.status is SourceStatus.READY
        assert source.indexed_count == 0
        assert source.last_error is None
        assert source.last_synced_at is not None

        docs = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert docs == []


async def test_sync_missing_source_is_noop(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, _source_id = await _seed_source()
    _patch_sync(monkeypatch, [])
    # A source id that does not exist in this tenant — idempotent no-op (error result).
    result = await sync_source_async(
        tenant_id,
        uuid.uuid4(),
        settings=_settings(),
        object_store=_FakeObjectStore(),  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
    )
    assert result.status is SourceStatus.ERROR
    assert result.error == "source not found"


# --- delete cleanup (regression for #139) -----------------------------------


async def _delete_source(tenant_id: uuid.UUID, owner_id: uuid.UUID, source_id: uuid.UUID) -> bool:
    """Delete a source via the service (mirrors the request path), then commit."""
    async with db_session.session_scope() as session:
        svc = SourcesService(
            session,
            tenant_id=tenant_id,
            owner_id=owner_id,
            object_store=_FakeObjectStore(),  # type: ignore[arg-type]
            audit=AuditSink(AuditEventRepository(session, tenant_id)),
            denials=denial_context(
                RecordingDurableAuditTransactions(), session, tenant_id, owner_id
            ),
            request_id="r",
            source_ip="203.0.113.1",
        )
        ok = await svc.delete(source_id)
        await session.commit()
    return ok


async def test_delete_source_leaves_no_orphans(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleting a source removes its docs, chunks, and backing collection (#139).

    Reported live: ``DELETE /sources/{id}`` removed the source row but left its
    auto-created ``web: <url>`` backing collection *and* the documents the sync
    ingested into it — junk that had to be cleared by hand. A source delete must
    cascade: source → its documents (+ chunks) → its backing collection.

    The assertions deliberately probe by **document id** and by **collection
    contents**, never by ``source_id``: the pre-fix ORM nulls ``documents.source_id``
    on the parent delete, so a ``list_for_source`` check would read empty (0 rows)
    even though the documents survive orphaned in the collection — a false pass.
    """
    tenant_id, source_id = await _seed_source()
    body = "The quick brown fox jumps over the lazy dog. " * 8
    _patch_sync(
        monkeypatch,
        [
            FetchedDoc(title="Page One", text=body, url="http://93.184.216.34/a"),
            FetchedDoc(title="Page Two", text=body, url="http://93.184.216.34/b"),
        ],
    )
    await _run(tenant_id, source_id)

    # Capture the owner + backing collection the sync populated (preconditions).
    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        owner_id = source.owner_id
        collection_id = uuid.UUID(str(source.config["collection_id"]))

        documents = DocumentRepository(session, tenant_id)
        doc_ids = [d.id for d in await documents.list_for_source(source_id)]
        assert len(doc_ids) == 2  # the sync ingested into the backing collection
        collections = CollectionRepository(session, tenant_id)
        assert await collections.get(collection_id) is not None
        assert await collections.count_documents(collection_id) == 2
        chunks = ChunkRepository(session, tenant_id)
        assert all([await chunks.list_for_document(doc_id) for doc_id in doc_ids])

    assert await _delete_source(tenant_id, owner_id, source_id) is True

    # No orphans: source gone, every ingested document + its chunks gone, and the
    # auto-created backing collection gone.
    async with db_session.session_scope() as session:
        assert await SourceRepository(session, tenant_id).get(source_id) is None
        documents = DocumentRepository(session, tenant_id)
        for doc_id in doc_ids:
            assert await documents.get(doc_id) is None
        collections = CollectionRepository(session, tenant_id)
        assert await collections.get(collection_id) is None
        assert await collections.count_documents(collection_id) == 0
        chunks = ChunkRepository(session, tenant_id)
        for doc_id in doc_ids:
            assert await chunks.list_for_document(doc_id) == []


async def test_delete_source_preserves_unrelated_docs_in_backing_collection(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source delete must not destroy a user's own uploads in its collection (#139 review).

    The ``web: <url>`` backing collection is an ordinary caller-owned, visible
    collection — uploads accept any owned ``collection_id``, so a user can put
    their own (``source_id = NULL``) documents in it. Deleting the source must
    remove only what the source ingested and **preserve** those unrelated
    documents (and the collection that now still holds them); it must not audit
    the preserved document as deleted.
    """
    tenant_id, source_id = await _seed_source()
    body = "The quick brown fox jumps over the lazy dog. " * 8
    _patch_sync(
        monkeypatch,
        [
            FetchedDoc(title="Page One", text=body, url="http://93.184.216.34/a"),
            FetchedDoc(title="Page Two", text=body, url="http://93.184.216.34/b"),
        ],
    )
    await _run(tenant_id, source_id)

    # Drop an unrelated direct upload (source_id = NULL) into the same caller-owned
    # backing collection, mirroring a user uploading into the visible collection.
    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        owner_id = source.owner_id
        collection_id = uuid.UUID(str(source.config["collection_id"]))
        documents = DocumentRepository(session, tenant_id)
        source_doc_ids = [d.id for d in await documents.list_for_source(source_id)]
        assert len(source_doc_ids) == 2
        unrelated = await documents.create(
            owner_id=owner_id,
            collection_id=collection_id,
            filename="my-own-upload.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            storage_key="tenant/abc/my-own-upload.pdf",
            acl_enforced=False,
            status=DocumentStatus.READY,
            source_id=None,
        )
        unrelated_id = unrelated.id
        assert await CollectionRepository(session, tenant_id).count_documents(collection_id) == 3
        await session.commit()

    assert await _delete_source(tenant_id, owner_id, source_id) is True

    async with db_session.session_scope() as session:
        # The source and its ingested documents are gone...
        assert await SourceRepository(session, tenant_id).get(source_id) is None
        documents = DocumentRepository(session, tenant_id)
        for doc_id in source_doc_ids:
            assert await documents.get(doc_id) is None
        # ...but the user's unrelated upload and its collection are preserved.
        assert await documents.get(unrelated_id) is not None
        collections = CollectionRepository(session, tenant_id)
        assert await collections.get(collection_id) is not None
        assert await collections.count_documents(collection_id) == 1

        # Audit: each source document is recorded ``document.deleted``; the
        # preserved upload is not, and ``source.deleted`` is still emitted.
        events = await AuditEventRepository(session, tenant_id).list_recent()
        deleted_doc_ids = {
            e.resource_id for e in events if e.action == AuditAction.DOCUMENT_DELETED.value
        }
        assert deleted_doc_ids == {str(d) for d in source_doc_ids}
        assert str(unrelated_id) not in deleted_doc_ids
        assert AuditAction.SOURCE_DELETED.value in {e.action for e in events}
