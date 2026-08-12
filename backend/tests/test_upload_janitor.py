"""Bounded abandoned-upload cleanup and COMPLETING recovery (#571)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.errors import DependencyError, NotFoundError
from app.db import models
from app.db.base import Base
from app.db.repositories import (
    CollectionRepository,
    DocumentUploadRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import DocumentUploadState, Role
from app.storage import StoredObjectMetadata
from app.tasks.upload_janitor import sweep_expired_uploads_async

import app.db.models  # noqa: F401  isort: skip


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, StoredObjectMetadata] = {}
        self.aborted: list[str] = []
        self.deleted: list[str] = []
        self.fail_abort_for: set[str] = set()

    async def head(self, tenant_id: str, key: str) -> StoredObjectMetadata:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise NotFoundError("object not found", code="object_not_found") from exc

    async def abort_multipart_upload(
        self, *, tenant_id: str, key: str, provider_upload_id: str
    ) -> None:
        if provider_upload_id in self.fail_abort_for:
            raise DependencyError("Object storage is unavailable.", code="storage_unavailable")
        self.aborted.append(provider_upload_id)

    async def delete(self, tenant_id: str, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


async def test_janitor_expires_abandoned_and_recovers_completed_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store = _Store()
    now = datetime.now(UTC)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def tenant_scope(_tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        async with scope() as session:
            yield session

    monkeypatch.setattr("app.tasks.upload_janitor.session_scope", scope)
    monkeypatch.setattr("app.tasks.upload_janitor.tenant_session_scope", tenant_scope)
    enqueued: list[tuple[uuid.UUID, uuid.UUID, bool]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_ingestion",
        lambda tenant_id, document_id, *, media: enqueued.append((tenant_id, document_id, media)),
    )

    try:
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Acme")
            owner = await UserRepository(session, tenant.id).create(
                email="owner@acme.test", password_hash="h", roles=[Role.MEMBER]
            )
            collection = await CollectionRepository(session, tenant.id).create(
                owner_id=owner.id, name="Media"
            )
            uploads = DocumentUploadRepository(session, tenant.id)
            abandoned_id = uuid.uuid4()
            abandoned_document_id = uuid.uuid4()
            abandoned_key = f"{tenant.id}/quarantine/{abandoned_document_id}/abandoned.mp3"
            await uploads.create(
                upload_id=abandoned_id,
                document_id=abandoned_document_id,
                owner_id=owner.id,
                collection_id=collection.id,
                filename="abandoned.mp3",
                mime_type="audio/mpeg",
                size_bytes=8,
                storage_key=abandoned_key,
                provider_upload_id="provider-abandoned",
                part_size_bytes=5 * 1024 * 1024,
                part_count=1,
                expires_at=now - timedelta(minutes=2),
            )
            recovery_id = uuid.uuid4()
            recovery_document_id = uuid.uuid4()
            recovery_key = f"{tenant.id}/quarantine/{recovery_document_id}/meeting.mp3"
            await uploads.create(
                upload_id=recovery_id,
                document_id=recovery_document_id,
                owner_id=owner.id,
                collection_id=collection.id,
                filename="meeting.mp3",
                mime_type="audio/mpeg",
                size_bytes=8,
                storage_key=recovery_key,
                provider_upload_id="provider-completed",
                part_size_bytes=5 * 1024 * 1024,
                part_count=1,
                expires_at=now - timedelta(minutes=1),
            )
            await uploads.set_state(recovery_id, owner.id, DocumentUploadState.COMPLETING)
            await session.commit()
            store.objects[recovery_key] = StoredObjectMetadata(
                key=recovery_key,
                size_bytes=8,
                content_type="audio/mpeg",
                metadata={
                    "lumen-upload-id": str(recovery_id),
                    "lumen-document-id": str(recovery_document_id),
                },
            )
            tenant_id = tenant.id
            owner_id = owner.id

        settings = get_settings().model_copy(update={"upload_janitor_batch_size": 10})
        result = await sweep_expired_uploads_async(
            now=now,
            settings=settings,
            object_store=store,  # type: ignore[arg-type]
        )

        assert (result.scanned, result.expired, result.recovered) == (2, 1, 1)
        assert store.aborted == ["provider-abandoned"]
        assert abandoned_key in store.deleted
        assert recovery_key not in store.deleted
        assert enqueued == [(tenant_id, recovery_document_id, True)]

        async with factory() as session:
            states = dict(
                (
                    await session.execute(
                        select(models.DocumentUpload.id, models.DocumentUpload.state)
                    )
                ).all()
            )
            assert states[abandoned_id] == DocumentUploadState.EXPIRED.value
            assert states[recovery_id] == DocumentUploadState.COMPLETED.value
            recovered_document = (
                await session.execute(
                    select(models.Document).where(
                        models.Document.id == recovery_document_id,
                        models.Document.owner_id == owner_id,
                    )
                )
            ).scalar_one()
            assert recovered_document.kind == "audio"
            actions = (await session.execute(select(models.AuditEvent.action))).scalars().all()
            assert actions.count(AuditAction.DOCUMENT_UPLOAD_EXPIRED.value) == 1
            assert actions.count(AuditAction.DOCUMENT_UPLOADED.value) == 1
    finally:
        await engine.dispose()


async def test_janitor_isolates_storage_failure_to_one_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    store = _Store()
    now = datetime.now(UTC)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def tenant_scope(_tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        async with scope() as session:
            yield session

    monkeypatch.setattr("app.tasks.upload_janitor.session_scope", scope)
    monkeypatch.setattr("app.tasks.upload_janitor.tenant_session_scope", tenant_scope)

    try:
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Acme")
            owner = await UserRepository(session, tenant.id).create(
                email="owner@acme.test", password_hash="h", roles=[Role.MEMBER]
            )
            collection = await CollectionRepository(session, tenant.id).create(
                owner_id=owner.id, name="Media"
            )
            uploads = DocumentUploadRepository(session, tenant.id)
            upload_ids: list[uuid.UUID] = []
            for provider_id in ("provider-unavailable", "provider-healthy"):
                upload_id = uuid.uuid4()
                document_id = uuid.uuid4()
                upload_ids.append(upload_id)
                await uploads.create(
                    upload_id=upload_id,
                    document_id=document_id,
                    owner_id=owner.id,
                    collection_id=collection.id,
                    filename=f"{provider_id}.mp3",
                    mime_type="audio/mpeg",
                    size_bytes=8,
                    storage_key=(f"{tenant.id}/quarantine/{document_id}/{provider_id}.mp3"),
                    provider_upload_id=provider_id,
                    part_size_bytes=5 * 1024 * 1024,
                    part_count=1,
                    expires_at=now - timedelta(minutes=1),
                )
            await session.commit()
        store.fail_abort_for.add("provider-unavailable")

        settings = get_settings().model_copy(update={"upload_janitor_batch_size": 10})
        result = await sweep_expired_uploads_async(
            now=now,
            settings=settings,
            object_store=store,  # type: ignore[arg-type]
        )

        assert (result.scanned, result.expired, result.recovered) == (2, 1, 0)
        assert store.aborted == ["provider-healthy"]
        async with factory() as session:
            rows = dict(
                (
                    await session.execute(
                        select(models.DocumentUpload.id, models.DocumentUpload.state)
                    )
                ).all()
            )
            assert rows[upload_ids[0]] == DocumentUploadState.INITIATED.value
            assert rows[upload_ids[1]] == DocumentUploadState.EXPIRED.value
    finally:
        await engine.dispose()
