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
    DocumentRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role, Source, SourceStatus
from app.domain.llm import Embedding
from app.services.audit import AuditSink
from app.services.sources_service import SourcesService
from app.tasks.sync_source import sync_source_async

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

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


class _FakeGateway:
    """Fake LLM gateway: a deterministic vector per input (offline)."""

    async def embed(self, inputs: Sequence[str], *, model: str | None = None) -> list[Embedding]:
        return [Embedding(vector=[float(len(t) % 5)] * _DIM, model="fake") for t in inputs]


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
    db_session._sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
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
            audit=AuditSink(AuditEventRepository(session, tenant.id)),
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

    async def _fake_sync(self: object, source: Source) -> Iterable[FetchedDoc]:
        if isinstance(docs, Exception):
            raise docs
        return list(docs)

    monkeypatch.setattr("app.connectors.web.connector.WebConnector.sync", _fake_sync)


async def _run(tenant_id: uuid.UUID, source_id: uuid.UUID) -> None:
    await sync_source_async(
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
