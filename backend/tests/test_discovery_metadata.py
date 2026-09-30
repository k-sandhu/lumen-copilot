"""Public connector metadata survives document persistence and sync."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.connectors.base import FetchedDoc
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    DocumentRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role, SourceStatus
from app.domain.llm import Embedding
from app.services.audit import AuditSink
from app.services.sources_service import SourcesService
from app.tasks.sync_source import sync_source_async

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Reports/report.txt?token=private#section", "Reports/report.txt"),
        ("https://[invalid/report?token=private", ""),
    ],
)
def test_optional_public_path_never_preserves_secrets_or_breaks_sync(
    raw: str,
    expected: str,
) -> None:
    from app.tasks.sync_source import _public_source_path

    assert _public_source_path(raw) == expected


_DIM = 8


class _ObjectStore:
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
        self.objects.pop(key, None)


class _Gateway:
    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        return [Embedding(vector=[float(len(text) % 5)] * _DIM, model="test") for text in inputs]


class _IndexStore:
    @classmethod
    def from_settings(cls, settings: object) -> _IndexStore:
        return cls()

    async def ensure_index(self) -> None: ...

    async def upsert_chunks(self, chunks: Sequence[object], *, refresh: bool = False) -> None: ...

    async def delete_document(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID, refresh: bool = False
    ) -> None: ...

    async def aclose(self) -> None: ...


@pytest_asyncio.fixture
async def sqlite_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    previous_engine, previous_factory = db_session._engine, db_session._sessionmaker
    db_session._engine = engine
    db_session._sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", _IndexStore)
    monkeypatch.setattr("app.tasks.enqueue_source_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.services.sources_service._dispatch_off_loop", lambda fn, *, name: fn())
    try:
        yield
    finally:
        db_session._engine, db_session._sessionmaker = previous_engine, previous_factory
        await engine.dispose()


async def _seed_source() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    object_store = _ObjectStore()
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Metadata Tenant")
        user = await UserRepository(session, tenant.id).create(
            email="metadata@example.test", password_hash="x", roles=[Role.MEMBER]
        )
        service = SourcesService(
            session,
            tenant_id=tenant.id,
            owner_id=user.id,
            object_store=object_store,  # type: ignore[arg-type]
            audit=AuditSink(AuditEventRepository(session, tenant.id)),
            request_id="metadata-test",
            source_ip="127.0.0.1",
        )
        source = await service.add(source_type="web", url="http://93.184.216.34/metadata-test")
        await session.commit()
        return tenant.id, user.id, source.id


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="sqlite+aiosqlite://",
        REDIS_URL="redis://localhost:1/0",
        CELERY_BROKER_URL="redis://localhost:1/1",
        CELERY_RESULT_BACKEND="redis://localhost:1/2",
        S3_ENDPOINT_URL="http://localhost:1",
        S3_ACCESS_KEY="unused",
        S3_SECRET_KEY="unused",
        S3_BUCKET="unused",
        OPENROUTER_API_KEY="",
        INGESTION_CHUNK_SIZE="120",
        INGESTION_CHUNK_OVERLAP="20",
        INGESTION_EMBED_BATCH_SIZE="3",
        LLM_EMBEDDING_DIMENSIONS=str(_DIM),
    )


async def test_document_public_metadata_round_trips_create_and_sync_update(
    sqlite_db: None,
) -> None:
    tenant_id, owner_id, source_id = await _seed_source()
    modified = datetime(2025, 4, 3, 12, 30, tzinfo=UTC)
    async with db_session.session_scope() as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        collection_id = uuid.UUID(str(source.config["collection_id"]))
        repo = DocumentRepository(session, tenant_id)
        created = await repo.create(
            owner_id=owner_id,
            collection_id=collection_id,
            filename="0000-report.txt",
            mime_type="text/plain",
            size_bytes=10,
            storage_key="tenant/object",
            acl_enforced=False,
            source_id=source_id,
            external_id="provider-1",
            title="Original title",
            source_path="https://example.test/reports/1",
            source_modified_at=modified,
            discovery_metadata={"mime_type": "text/plain", "external_id": "provider-1"},
        )
        assert created.title == "Original title"
        assert created.source_path == "https://example.test/reports/1"
        assert created.source_modified_at == modified
        assert created.discovery_metadata == {
            "mime_type": "text/plain",
            "external_id": "provider-1",
        }

        updated = await repo.update_from_sync(
            created.id,
            filename="0000-report.txt",
            mime_type="application/pdf",
            size_bytes=20,
            storage_key="tenant/new-object",
            status=DocumentStatus.PENDING,
            acl_principals=None,
            acl_synced_at=None,
            acl_scope_ids=None,
            title="Updated title",
            source_path="https://example.test/reports/2",
            source_modified_at=modified,
            discovery_metadata={"mime_type": "application/pdf", "external_id": "provider-1"},
        )
        assert updated is not None
        assert updated.title == "Updated title"
        assert updated.source_path == "https://example.test/reports/2"
        assert updated.source_modified_at == modified.replace(tzinfo=None)
        assert updated.discovery_metadata == {
            "mime_type": "application/pdf",
            "external_id": "provider-1",
        }

        legacy_caller = await repo.create(
            owner_id=owner_id,
            collection_id=collection_id,
            filename="legacy.txt",
            mime_type="text/plain",
            size_bytes=0,
            storage_key="tenant/legacy",
            acl_enforced=False,
        )
        assert legacy_caller.title is None
        assert legacy_caller.source_path is None
        assert legacy_caller.source_modified_at is None
        assert legacy_caller.discovery_metadata is None


async def test_connector_sync_persists_fetched_public_metadata_without_url_secrets(
    sqlite_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, _, source_id = await _seed_source()
    modified = datetime(2025, 4, 3, 12, 30, tzinfo=UTC)

    async def fake_sync(self: object, source: object, run: object) -> list[FetchedDoc]:
        return [
            FetchedDoc(
                title="Fetched report",
                text="A sufficiently long body for ingestion. " * 8,
                url="https://user:secret@example.test/reports/1?token=private#section",
                external_id="provider-2",
                modified_at=modified,
            )
        ]

    monkeypatch.setattr("app.connectors.web.connector.WebConnector.sync", fake_sync)
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *args, **kwargs: None)
    result = await sync_source_async(
        tenant_id,
        source_id,
        settings=_settings(),
        object_store=_ObjectStore(),  # type: ignore[arg-type]
        gateway=_Gateway(),  # type: ignore[arg-type]
    )
    assert result.status is SourceStatus.READY

    async with db_session.session_scope() as session:
        documents = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert len(documents) == 1
        document = documents[0]
        assert document.title == "Fetched report"
        assert document.source_path == "https://example.test/reports/1"
        assert document.source_modified_at == modified.replace(tzinfo=None)
        assert document.discovery_metadata == {
            "mime_type": "text/plain",
            "external_id": "provider-2",
        }
