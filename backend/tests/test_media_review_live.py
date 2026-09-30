"""PR #606 regression proofs on an isolated PostgreSQL database and ordinary role.

Opt in with RUN_LIVE=1 and the exact DATABASE_URL below. This module resets only
lumentest_pr606 before each invocation and drops its database/role at teardown.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.tasks  # noqa: F401  isort: skip — initialize task registry before upload service

from app.api.v2.uploads import _commit_rejection
from app.core.config import get_settings
from app.core.errors import ValidationError
from app.db import models
from app.db.repositories import (
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
from app.domain.entities import DocumentUploadState, MessageRole, Role
from app.services.document_upload_service import CompletePartInput
from app.storage import UploadedPart
from app.tasks.upload_janitor import _recovery_service
from tests.test_direct_upload_api import FakeMultipartStore

_URL = "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumentest_pr606"
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


async def _admin(database: str = "lumentest_pr606") -> asyncpg.Connection:
    return await asyncpg.connect(
        user="lumen", password="lumen_local_dev", host="localhost", port=47182, database=database
    )


@pytest.fixture(scope="module")
def live_database() -> Iterator[str]:
    assert os.environ.get("DATABASE_URL") == _URL, "refusing any other live database"
    role = f"pr606_r2_{uuid.uuid4().hex}"

    async def reset() -> None:
        connection = await _admin("postgres")
        try:
            await connection.execute("DROP DATABASE IF EXISTS lumentest_pr606 WITH (FORCE)")
            await connection.execute("CREATE DATABASE lumentest_pr606")
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
            await connection.execute("DROP DATABASE IF EXISTS lumentest_pr606 WITH (FORCE)")
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
