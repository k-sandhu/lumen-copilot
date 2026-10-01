"""PR #606 regression proofs on an isolated PostgreSQL database and ordinary role.

Opt in with RUN_LIVE=1 and the exact DATABASE_URL below. This module resets only
lumentest_pr604 before each invocation and drops its database/role at teardown.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.tasks  # noqa: F401  isort: skip — initialize task registry before upload service

from app.api.v2.uploads import _commit_rejection
from app.auth.principal import Principal
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.db import models
from app.db.audit_transactions import DurableAuditTransactions
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    DocumentUploadRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction
from app.domain.entities import Document, DocumentUpload, DocumentUploadState, MessageRole, Role
from app.services.audit import AuditSink, PermissionDeniedContext, PermissionDeniedRecorder
from app.services.collections_service import CollectionsService
from app.services.document_upload_service import CompletePartInput
from app.storage import UploadedPart
from app.tasks.upload_janitor import _recovery_service, sweep_expired_uploads_async
from tests.test_direct_upload_api import FakeMultipartStore

_URL = "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumentest_pr604"
_BACKEND = Path(__file__).resolve().parents[1]
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE") != "1",
        reason="requires explicit isolated live PostgreSQL run",
    ),
]


def _config() -> Config:
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "alembic"))
    return cfg


async def _admin(database: str = "lumentest_pr604") -> asyncpg.Connection:
    return await asyncpg.connect(
        user="lumen", password="lumen_local_dev", host="localhost", port=47182, database=database
    )


@pytest.fixture(scope="module")
def live_database() -> Iterator[str]:
    assert os.environ.get("DATABASE_URL") == _URL, "refusing any other live database"
    role = f"pr604_r11_{uuid.uuid4().hex}"

    async def reset() -> None:
        connection = await _admin("postgres")
        try:
            await connection.execute("DROP DATABASE IF EXISTS lumentest_pr604 WITH (FORCE)")
            await connection.execute("CREATE DATABASE lumentest_pr604")
        finally:
            await connection.close()

    async def grant() -> None:
        connection = await _admin()
        try:
            await connection.execute(f"CREATE ROLE {role} NOSUPERUSER NOBYPASSRLS NOLOGIN")
            await connection.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            await connection.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}"
            )
            await connection.execute(f"REVOKE UPDATE, DELETE ON audit_events FROM {role}")
        finally:
            await connection.close()

    async def cleanup() -> None:
        connection = await _admin("postgres")
        try:
            await connection.execute("DROP DATABASE IF EXISTS lumentest_pr604 WITH (FORCE)")
            await connection.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await connection.close()

    asyncio.run(reset())
    get_settings.cache_clear()
    try:
        command.upgrade(_config(), "head")
        asyncio.run(grant())
        yield role
    finally:
        asyncio.run(cleanup())


@pytest_asyncio.fixture
async def restricted_session(live_database: str) -> AsyncIterator[AsyncSession]:
    # Populated downgrade/re-upgrade recreates tables, so refresh fixture grants.
    admin = await _admin()
    try:
        await admin.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f"TO {live_database}"
        )
        await admin.execute(f"REVOKE UPDATE, DELETE ON audit_events FROM {live_database}")
    finally:
        await admin.close()
    engine = create_async_engine(_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f"SET ROLE {live_database}"))
            await connection.commit()
            assert (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one() == (False, False)
            await connection.commit()
            try:
                async with AsyncSession(connection, expire_on_commit=False) as session:
                    yield session
            finally:
                await connection.rollback()
                await connection.execute(text("RESET ROLE"))
                await connection.commit()
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def durable_audit_transactions(
    live_database: str,
    monkeypatch: pytest.MonkeyPatch,
    _wire_offline_durable_audit: None,
) -> AsyncIterator[DurableAuditTransactions]:
    """Use a separate real audit pool under the same restricted application role."""
    engine = create_async_engine(_URL)

    @event.listens_for(engine.sync_engine, "begin")
    def restrict_role(connection: Connection) -> None:
        # Reapply on every transaction, including after a prior audit rollback.
        connection.execute(text(f"SET LOCAL ROLE {live_database}"))
        assert connection.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one() == (False, False)

    provider = DurableAuditTransactions(engine, operation_timeout_seconds=10)
    monkeypatch.setattr(
        "app.db.session.get_durable_audit_transactions", lambda settings=None: provider
    )
    monkeypatch.setattr(
        "app.api.deps.get_durable_audit_transactions", lambda settings=None: provider
    )
    try:
        yield provider
    finally:
        await provider.dispose()


def _collection_denials(
    session: AsyncSession,
    upload: DocumentUpload,
    provider: DurableAuditTransactions,
    request_id: str,
) -> PermissionDeniedContext:
    return PermissionDeniedContext(
        PermissionDeniedRecorder(provider, tenant_id=upload.tenant_id, request_session=session),
        principal=Principal(upload.owner_id, upload.tenant_id, (Role.MEMBER,)),
        request_id=request_id,
        source_ip="127.0.0.1",
    )


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = create_async_engine(_URL)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            tenant = await TenantRepository(session).create(name="R2 regression")
            owner = await UserRepository(session, tenant.id).create(
                email=f"{uuid.uuid4()}@rls.test",
                password_hash="h",
                roles=[Role.MEMBER],
            )
            collection = await CollectionRepository(session, tenant.id).create(
                owner_id=owner.id, name="Media"
            )
            await session.commit()
            return tenant.id, owner.id, collection.id
    finally:
        await engine.dispose()


def test_populated_media_downgrade_preserves_large_byte_counts(live_database: str) -> None:
    sizes = [2**31 - 1, 2**31, 3 * 1024**3]
    ids: list[uuid.UUID] = []

    async def seed() -> None:
        tenant, owner, collection = await _seed()
        connection = await _admin()
        try:
            for size in sizes:
                document_id = uuid.uuid4()
                ids.append(document_id)
                await connection.execute(
                    "INSERT INTO documents (id, tenant_id, owner_id, collection_id, filename, "
                    "mime_type, size_bytes, storage_key, status, acl_enforced) "
                    "VALUES ($1,$2,$3,$4,'large.mp3','audio/mpeg',$5,$6,'pending',false)",
                    document_id,
                    tenant,
                    owner,
                    collection,
                    size,
                    f"{tenant}/{document_id}",
                )
        finally:
            await connection.close()

    async def read_back() -> None:
        connection = await _admin()
        try:
            for document_id, size in zip(ids, sizes, strict=True):
                assert (
                    await connection.fetchval(
                        "SELECT size_bytes FROM documents WHERE id=$1", document_id
                    )
                    == size
                )
            assert (
                await connection.fetchval(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name='documents' AND column_name='size_bytes'"
                )
                == "bigint"
            )
        finally:
            await connection.close()

    asyncio.run(seed())
    try:
        command.downgrade(_config(), "0043_code_run_resolved_packages")
        asyncio.run(read_back())
    finally:
        command.upgrade(_config(), "head")


@pytest.mark.parametrize("table", ["chunks", "citations"])
@pytest.mark.parametrize("start,end", [(None, 1_000), (0, None), (1_000, 0)])
async def test_timestamp_pair_dml_rejected_under_rls(
    restricted_session: AsyncSession,
    table: str,
    start: int | None,
    end: int | None,
) -> None:
    tenant, owner, collection = await _seed()
    session = restricted_session
    await bind_tenant(session, tenant)
    document = await DocumentRepository(session, tenant).create(
        owner_id=owner,
        collection_id=collection,
        filename="plain.txt",
        mime_type="text/plain",
        size_bytes=8,
        storage_key=f"{tenant}/plain.txt",
        acl_enforced=False,
    )
    chunk = await ChunkRepository(session, tenant).add(
        document_id=document.id,
        ord=0,
        text="x",
        char_start=0,
        char_end=1,
    )
    chat = await ChatSessionRepository(session, tenant).create(owner_id=owner, model="fake")
    message = await MessageRepository(session, tenant).add(
        session_id=chat.id,
        role=MessageRole.ASSISTANT,
        content="x",
    )
    # Both-null is valid. Explicit DML also bypasses repository validation.
    session.add(
        models.Citation(
            tenant_id=tenant, message_id=message.id, chunk_id=chunk.id, char_start=0, char_end=1
        )
    )
    await session.flush()
    timed_chunk = await ChunkRepository(session, tenant).add(
        document_id=document.id,
        ord=1,
        text="x",
        char_start=0,
        char_end=1,
        time_start_ms=0,
        time_end_ms=1_000,
    )
    session.add(
        models.Citation(
            tenant_id=tenant,
            message_id=message.id,
            chunk_id=timed_chunk.id,
            char_start=0,
            char_end=1,
            time_start_ms=0,
            time_end_ms=1_000,
        )
    )
    await session.flush()
    with pytest.raises(IntegrityError, match=f"ck_{table}_time_span"):
        async with session.begin_nested():
            if table == "chunks":
                row = models.Chunk(
                    tenant_id=tenant,
                    document_id=document.id,
                    ord=2,
                    text="x",
                    char_start=0,
                    char_end=1,
                    time_start_ms=start,
                    time_end_ms=end,
                )
            else:
                row = models.Citation(
                    tenant_id=tenant,
                    message_id=message.id,
                    chunk_id=chunk.id,
                    char_start=0,
                    char_end=1,
                    time_start_ms=start,
                    time_end_ms=end,
                )
            session.add(row)
            await session.flush()
    await session.commit()


async def test_complete_rebinds_tenant_after_durable_commit(
    restricted_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, owner, collection = await _seed()
    session = restricted_session
    await bind_tenant(session, tenant)
    upload_id, document_id = uuid.uuid4(), uuid.uuid4()
    key = f"{tenant}/quarantine/{document_id}/test.mp3"
    upload = await DocumentUploadRepository(session, tenant).create(
        upload_id=upload_id,
        document_id=document_id,
        owner_id=owner,
        collection_id=collection,
        filename="test.mp3",
        mime_type="audio/mpeg",
        size_bytes=8,
        storage_key=key,
        provider_upload_id="provider",
        part_size_bytes=5 * 1024**2,
        part_count=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await session.commit()
    store = FakeMultipartStore()
    store.uploads["provider"] = {
        "key": key,
        "content_type": "audio/mpeg",
        "metadata": {"lumen-upload-id": str(upload_id), "lumen-document-id": str(document_id)},
        "parts": [UploadedPart(part_number=1, etag='"etag-1"', size_bytes=8)],
    }
    enqueued: list[uuid.UUID] = []
    monkeypatch.setattr("app.tasks.enqueue_ingestion", lambda t, d, **kw: enqueued.append(d))
    await bind_tenant(session, tenant)
    service = _recovery_service(
        session=session, candidate=upload, store=store, settings=get_settings()
    )
    result = await service.complete(upload_id, [CompletePartInput(part_number=1, etag='"etag-1"')])
    assert result is not None and result.id == document_id
    await session.commit()
    await bind_tenant(session, tenant)
    reloaded = await DocumentUploadRepository(session, tenant).get_for_owner(upload_id, owner)
    assert reloaded is not None and reloaded.state is DocumentUploadState.COMPLETED
    assert store.complete_calls == ["provider"] and enqueued == [document_id]
    assert (
        len(
            (
                await session.execute(
                    select(models.AuditEvent).where(
                        models.AuditEvent.tenant_id == tenant,
                        models.AuditEvent.resource_id == str(document_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        == 1
    )
    assert (await service.complete(upload_id, [])).id == document_id
    await session.commit()
    await bind_tenant(session, uuid.uuid4())
    # No repository predicate: the database itself must withhold this upload.
    assert (
        await session.execute(
            select(models.DocumentUpload).where(
                models.DocumentUpload.id == upload_id,
            )
        )
    ).scalar_one_or_none() is None


async def test_rejection_audit_rebinds_tenant_after_rollback(
    restricted_session: AsyncSession,
) -> None:
    tenant, owner, collection = await _seed()
    session = restricted_session
    await bind_tenant(session, tenant)
    upload = await DocumentUploadRepository(session, tenant).create(
        upload_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        owner_id=owner,
        collection_id=collection,
        filename="test.mp3",
        mime_type="audio/mpeg",
        size_bytes=8,
        storage_key=f"{tenant}/test.mp3",
        provider_upload_id="private",
        part_size_bytes=5 * 1024**2,
        part_count=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await session.commit()
    await bind_tenant(session, tenant)
    service = _recovery_service(
        session=session, candidate=upload, store=FakeMultipartStore(), settings=get_settings()
    )
    # Pending mutation must roll back while the rejection event commits.
    await DocumentUploadRepository(session, tenant).set_state(
        upload.id, owner, DocumentUploadState.COMPLETING
    )
    error = ValidationError("invalid part", code="invalid_part_number")
    await _commit_rejection(
        session,
        service,
        operation="sign_parts",
        resource_type="document_upload",
        resource_id=upload.id,
        error=error,
    )
    await bind_tenant(session, tenant)
    reloaded = await DocumentUploadRepository(session, tenant).get_for_owner(upload.id, owner)
    assert reloaded is not None and reloaded.state is DocumentUploadState.INITIATED
    event = (
        await session.execute(
            select(models.AuditEvent).where(
                models.AuditEvent.tenant_id == tenant,
                models.AuditEvent.resource_id == str(upload.id),
            )
        )
    ).scalar_one()
    assert event.outcome == "error"
    assert event.event_metadata == {
        "operation": "sign_parts",
        "reason_code": error.code,
        "status": 422,
    }


async def _live_upload(
    session: AsyncSession, store: FakeMultipartStore, *, expires_at: datetime
) -> DocumentUpload:
    tenant, owner, collection = await _seed()
    await bind_tenant(session, tenant)
    upload_id, document_id = uuid.uuid4(), uuid.uuid4()
    provider_id = f"provider-{upload_id}"
    key = f"{tenant}/quarantine/{document_id}/test.mp3"
    upload = await DocumentUploadRepository(session, tenant).create(
        upload_id=upload_id,
        document_id=document_id,
        owner_id=owner,
        collection_id=collection,
        filename="test.mp3",
        mime_type="audio/mpeg",
        size_bytes=8,
        storage_key=key,
        provider_upload_id=provider_id,
        part_size_bytes=5 * 1024**2,
        part_count=1,
        expires_at=expires_at,
    )
    await session.commit()
    store.uploads[provider_id] = {
        "key": key,
        "content_type": "audio/mpeg",
        "metadata": {"lumen-upload-id": str(upload_id), "lumen-document-id": str(document_id)},
        "parts": [UploadedPart(part_number=1, etag='"etag-1"', size_bytes=8)],
    }
    return upload


async def test_janitor_invalid_parts_terminalize_under_rls_and_continue_other_tenant(
    restricted_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    session, store = restricted_session, FakeMultipartStore()
    now = datetime.now(UTC)
    corrupt = await _live_upload(session, store, expires_at=now - timedelta(minutes=2))
    await bind_tenant(session, corrupt.tenant_id)
    await DocumentUploadRepository(session, corrupt.tenant_id).set_state(
        corrupt.id, corrupt.owner_id, DocumentUploadState.COMPLETING
    )
    await session.commit()
    other = await _live_upload(session, store, expires_at=now - timedelta(minutes=1))
    store.upload_parts(corrupt.provider_upload_id, [7])
    monkeypatch.setattr("app.tasks.enqueue_ingestion", lambda *a, **kw: pytest.fail("corrupt"))

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    @asynccontextmanager
    async def tenant_scope(tenant: uuid.UUID) -> AsyncIterator[AsyncSession]:
        await bind_tenant(session, tenant)
        async with scope() as scoped:
            yield scoped

    @asynccontextmanager
    async def discovery_scope() -> AsyncIterator[AsyncSession]:
        # Global discovery is the deliberate privileged system read. Every
        # terminal mutation/audit below uses the asserted non-bypass role.
        engine = create_async_engine(_URL)
        try:
            async with AsyncSession(engine) as discovery:
                yield discovery
                await discovery.commit()
        finally:
            await engine.dispose()

    monkeypatch.setattr("app.tasks.upload_janitor.session_scope", discovery_scope)
    monkeypatch.setattr("app.tasks.upload_janitor.tenant_session_scope", tenant_scope)
    result = await sweep_expired_uploads_async(now=now, object_store=store)  # type: ignore[arg-type]
    assert (result.scanned, result.expired, result.recovered) == (2, 2, 0)
    assert store.aborted == [corrupt.provider_upload_id, other.provider_upload_id]
    assert store.deleted == [corrupt.storage_key, other.storage_key]
    assert store.complete_calls == []
    for upload in (corrupt, other):
        await bind_tenant(session, upload.tenant_id)
        saved = await DocumentUploadRepository(session, upload.tenant_id).get_for_owner(
            upload.id, upload.owner_id
        )
        assert saved is not None and saved.state is DocumentUploadState.EXPIRED
        assert saved.error == (
            "invalid_provider_part_layout" if upload is corrupt else "upload_session_expired"
        )
        event = (
            await session.execute(
                select(models.AuditEvent).where(models.AuditEvent.resource_id == str(upload.id))
            )
        ).scalar_one()
        assert event.action == AuditAction.DOCUMENT_UPLOAD_EXPIRED.value
        assert event.source_origin == "system" and event.outcome == "error"
        assert event.event_metadata == {
            "document_id": str(upload.document_id),
            **({"reason_code": "invalid_provider_part_layout"} if upload is corrupt else {}),
        }
        await session.commit()
    await bind_tenant(session, corrupt.tenant_id)
    # Direct DML read without repository predicates still enforces tenant isolation.
    assert (
        await session.scalar(
            select(models.DocumentUpload).where(models.DocumentUpload.id == other.id)
        )
        is None
    )
    await session.commit()
    again = await sweep_expired_uploads_async(now=now, object_store=store)  # type: ignore[arg-type]
    assert again.scanned == 0 and len(store.aborted) == 2


async def test_collection_delete_at_durable_completion_boundary_under_rls(
    restricted_session: AsyncSession,
    live_database: str,
    monkeypatch: pytest.MonkeyPatch,
    durable_audit_transactions: DurableAuditTransactions,
) -> None:
    session, store = restricted_session, FakeMultipartStore()
    upload = await _live_upload(session, store, expires_at=datetime.now(UTC) + timedelta(hours=1))
    await bind_tenant(session, upload.tenant_id)
    service = _recovery_service(
        session=session, candidate=upload, store=store, settings=get_settings()
    )  # type: ignore[arg-type]
    committed, deleted = asyncio.Event(), asyncio.Event()
    original_commit = session.commit

    async def commit_gap() -> None:
        await original_commit()
        committed.set()
        await deleted.wait()

    monkeypatch.setattr(session, "commit", commit_gap)
    monkeypatch.setattr("app.tasks.enqueue_ingestion", lambda *a, **kw: pytest.fail("deleted"))

    async def delete() -> None:
        await committed.wait()
        engine = create_async_engine(_URL)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f"SET ROLE {live_database}"))
                await connection.commit()
                assert (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).one() == (False, False)
                await connection.commit()
                async with AsyncSession(connection, expire_on_commit=False) as deleter:
                    await bind_tenant(deleter, upload.tenant_id)
                    current = await DocumentUploadRepository(
                        deleter, upload.tenant_id
                    ).get_for_owner(upload.id, upload.owner_id)
                    assert current is not None and current.state is DocumentUploadState.COMPLETING
                    collections = CollectionsService(
                        deleter,
                        tenant_id=upload.tenant_id,
                        owner_id=upload.owner_id,
                        object_store=store,  # type: ignore[arg-type]
                        audit=AuditSink(AuditEventRepository(deleter, upload.tenant_id)),
                        denials=_collection_denials(
                            deleter, upload, durable_audit_transactions, "r3-delete"
                        ),
                    )
                    assert await collections.delete(upload.collection_id)
                    await deleter.commit()
        finally:
            await engine.dispose()
            deleted.set()

    result, _ = await asyncio.gather(
        service.complete(upload.id, [CompletePartInput(1, '"etag-1"')]), delete()
    )
    assert result is None
    assert store.aborted == [upload.provider_upload_id] and store.complete_calls == []
    assert store.deleted == [upload.storage_key]
    # The router's actual rejection transaction must still work after the gap.
    await _commit_rejection(
        session,
        service,
        operation="complete",
        resource_type="document_upload",
        resource_id=upload.id,
        error=NotFoundError("Upload not found."),
        permission_denied=True,
    )
    await bind_tenant(session, upload.tenant_id)
    assert (
        await DocumentUploadRepository(session, upload.tenant_id).get_for_owner(
            upload.id, upload.owner_id
        )
        is None
    )
    await session.rollback()
    # Read through the independent restricted pool, after the caller rolled back.
    async with AsyncSession(durable_audit_transactions.engine) as reader:
        await bind_tenant(reader, upload.tenant_id)
        event = (
            await reader.execute(
                select(models.AuditEvent).where(
                    models.AuditEvent.resource_id == str(upload.id),
                    models.AuditEvent.request_id == "upload-janitor",
                )
            )
        ).scalar_one()
        await bind_tenant(reader, uuid.uuid4())
        assert (
            await reader.scalar(select(models.AuditEvent).where(models.AuditEvent.id == event.id))
            is None
        )
    assert (event.action, event.outcome) == (AuditAction.PERMISSION_DENIED.value, "denied")
    assert event.tenant_id == upload.tenant_id and event.actor_id is None
    assert event.request_id == "upload-janitor"
    assert event.source_origin == "system" and event.source_ip is None
    assert event.event_metadata == {
        "attempted_action": "document_upload.complete",
        "reason": "not_visible",
    }


@pytest.mark.parametrize("mode", ["complete", "recover_parts", "recover_head", "janitor"])
async def test_finalization_and_collection_delete_do_not_deadlock(
    restricted_session: AsyncSession,
    live_database: str,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    durable_audit_transactions: DurableAuditTransactions,
) -> None:
    """Hold finalization before its FK insert; prove deletion waits, then release.

    The database's blocker graph is the handshake: with the old order deletion
    waits on the upload while holding its parent, and releasing the insert forms
    a deadlock. With parent-first locks deletion waits on the parent instead.
    No sleeps, deadlines, or elapsed-time assertions decide the interleaving.
    """
    session, store = restricted_session, FakeMultipartStore()
    now = datetime.now(UTC)
    upload = await _live_upload(
        session,
        store,
        expires_at=now + (timedelta(hours=1) if mode != "janitor" else -timedelta(minutes=1)),
    )
    await bind_tenant(session, upload.tenant_id)
    if mode != "complete":
        await DocumentUploadRepository(session, upload.tenant_id).set_state(
            upload.id, upload.owner_id, DocumentUploadState.COMPLETING
        )
        await session.commit()
        await bind_tenant(session, upload.tenant_id)
    if mode == "recover_head":
        await store.complete_multipart_upload(provider_upload_id=upload.provider_upload_id)

    completion_pid = await session.scalar(text("SELECT pg_backend_pid()"))
    finalizing, deletion_started, release_insert = asyncio.Event(), asyncio.Event(), asyncio.Event()
    deletion_pid: int | None = None
    original_create = DocumentRepository.create

    async def gated_create(repo: DocumentRepository, **kwargs: object) -> Document:
        if kwargs.get("document_id") == upload.document_id:
            finalizing.set()
            await release_insert.wait()
        return await original_create(repo, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(DocumentRepository, "create", gated_create)
    enqueued: list[uuid.UUID] = []
    indexed: list[uuid.UUID] = []
    monkeypatch.setattr("app.tasks.enqueue_ingestion", lambda t, d, **kw: enqueued.append(d))
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda t, d: indexed.append(d))
    service = _recovery_service(
        session=session, candidate=upload, store=store, settings=get_settings()
    )  # type: ignore[arg-type]

    @asynccontextmanager
    async def tenant_scope(tenant: uuid.UUID) -> AsyncIterator[AsyncSession]:
        await bind_tenant(session, tenant)
        yield session
        await session.commit()

    @asynccontextmanager
    async def discovery_scope() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(_URL)
        try:
            async with AsyncSession(engine) as discovery:
                yield discovery
        finally:
            await engine.dispose()

    monkeypatch.setattr("app.tasks.upload_janitor.tenant_session_scope", tenant_scope)
    monkeypatch.setattr("app.tasks.upload_janitor.session_scope", discovery_scope)

    async def complete() -> object:
        try:
            if mode == "janitor":
                result = await sweep_expired_uploads_async(now=now, object_store=store)  # type: ignore[arg-type]
                assert (result.scanned, result.recovered, result.expired) == (1, 1, 0)
                return result
            result = (
                await service.complete(upload.id, [CompletePartInput(1, '"etag-1"')])
                if mode == "complete"
                else await service.recover_completing(upload.id)
            )
            assert result is not None and result.id == upload.document_id
            await session.commit()
            return result
        except BaseException:
            await session.rollback()
            raise

    async def delete() -> bool:
        nonlocal deletion_pid
        await finalizing.wait()
        engine = create_async_engine(_URL)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(f"SET ROLE {live_database}"))
                await connection.commit()
                assert (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles "
                            "WHERE rolname = current_user"
                        )
                    )
                ).one() == (False, False)
                deletion_pid = await connection.scalar(text("SELECT pg_backend_pid()"))
                await connection.commit()
                async with AsyncSession(connection, expire_on_commit=False) as deleter:
                    await bind_tenant(deleter, upload.tenant_id)
                    deletion_started.set()
                    collections = CollectionsService(
                        deleter,
                        tenant_id=upload.tenant_id,
                        owner_id=upload.owner_id,
                        object_store=store,  # type: ignore[arg-type]
                        audit=AuditSink(AuditEventRepository(deleter, upload.tenant_id)),
                        denials=_collection_denials(
                            deleter, upload, durable_audit_transactions, "r4-concurrent-delete"
                        ),
                    )
                    result = await collections.delete(upload.collection_id)
                    await deleter.commit()
                    return result
        finally:
            deletion_started.set()
            await engine.dispose()

    completing, deleting = asyncio.create_task(complete()), asyncio.create_task(delete())
    try:
        await finalizing.wait()
        await deletion_started.wait()
        assert deletion_pid is not None and completion_pid is not None
        observer = await _admin()
        try:
            while completion_pid not in await observer.fetchval(
                "SELECT pg_blocking_pids($1)", deletion_pid
            ):
                assert not deleting.done(), "deletion must contend with locked finalization"
        finally:
            await observer.close()
        release_insert.set()
        results = await asyncio.gather(completing, deleting, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        assert not errors, f"concurrent finalization/delete failed: {errors}"
        assert results[1] is True
    finally:
        release_insert.set()
        for task in (completing, deleting):
            if not task.done():
                task.cancel()
        await asyncio.gather(completing, deleting, return_exceptions=True)

    await bind_tenant(session, upload.tenant_id)
    assert await CollectionRepository(session, upload.tenant_id).get(upload.collection_id) is None
    assert await DocumentRepository(session, upload.tenant_id).get(upload.document_id) is None
    assert (
        await DocumentUploadRepository(session, upload.tenant_id).get_for_owner(
            upload.id, upload.owner_id
        )
        is None
    )
    events = (
        (
            await session.execute(
                select(models.AuditEvent).where(
                    models.AuditEvent.resource_id == str(upload.document_id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(event.action for event in events) == sorted(
        [AuditAction.DOCUMENT_UPLOADED.value, AuditAction.DOCUMENT_DELETED.value]
    )
    assert store.complete_calls == [upload.provider_upload_id]
    assert store.deleted == [upload.storage_key] and store.aborted == []
    assert enqueued == indexed == [upload.document_id]
