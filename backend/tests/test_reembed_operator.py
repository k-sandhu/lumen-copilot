"""Transactional, resumable re-embedding operator tests (R1-008)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.models as models
import app.db.session as db_session
import app.ingestion.reembed as reembed_module
from app.core.config import get_settings
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


@pytest_asyncio.fixture
async def operator_db() -> AsyncIterator[None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    previous_engine = db_session._engine
    previous_maker = db_session._sessionmaker
    db_session._engine = engine
    db_session._sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield
    finally:
        db_session._engine = previous_engine
        db_session._sessionmaker = previous_maker
        await engine.dispose()


async def _seed_candidates(count: int) -> tuple[UUID, list[UUID]]:
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Operator tenant")
        owner = await UserRepository(session, tenant.id).create(
            email="operator@example.test", password_hash="h", roles=[Role.ADMIN]
        )
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=owner.id, name="Legacy"
        )
        ids = []
        for index in range(count):
            document = await DocumentRepository(session, tenant.id).create(
                owner_id=owner.id,
                collection_id=collection.id,
                filename=f"legacy-{index}.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key=f"legacy-{index}",
                acl_enforced=False,
                status=DocumentStatus.READY,
            )
            [chunk] = await ChunkRepository(session, tenant.id).replace_for_document(
                document.id,
                [ChunkInput(text="legacy", char_start=0, char_end=6)],
            )
            chunk_row = await session.get(models.Chunk, chunk.id)
            assert chunk_row is not None
            chunk_row.legacy_embedding = [0.25] * 1024
            ids.append(document.id)
        return tenant.id, ids


async def test_preview_is_read_only(operator_db: None, capsys: pytest.CaptureFixture[str]) -> None:
    tenant_id, document_ids = await _seed_candidates(2)

    candidates = await reembed_module._candidates(limit=20, tenant_id=tenant_id)

    assert [document_id for _tenant, document_id in candidates] == sorted(document_ids)
    async with db_session.session_scope() as session:
        statuses = []
        for document_id in document_ids:
            document = await DocumentRepository(session, tenant_id).get(document_id)
            assert document is not None
            statuses.append(document.status)
    assert statuses == [DocumentStatus.READY, DocumentStatus.READY]
    assert capsys.readouterr().out == ""


async def test_same_width_foreign_model_is_selected_until_reembedded(
    operator_db: None,
) -> None:
    """R1-006: fingerprint drift, not only missing native bytes, drives backfill."""

    tenant_id, [document_id] = await _seed_candidates(1)
    target_fingerprint = get_settings().embedding_space_fingerprint
    async with db_session.session_scope() as session:
        [chunk] = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        row = await session.get(models.Chunk, chunk.id)
        assert row is not None
        row.embedding = [0.5] * 2048
        row.legacy_embedding = None
        row.embedding_fingerprint = "e" * 64

    candidates = await reembed_module._candidates(limit=20, tenant_id=tenant_id)
    assert candidates == [(tenant_id, document_id)]

    async with db_session.session_scope() as session:
        [chunk] = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        row = await session.get(models.Chunk, chunk.id)
        assert row is not None
        row.embedding_fingerprint = target_fingerprint

    assert await reembed_module._candidates(limit=20, tenant_id=tenant_id) == []


async def test_parallel_operators_reserve_disjoint_pages(operator_db: None) -> None:
    """R1-008: concurrent operator runs never publish the same reservation."""

    tenant_id, document_ids = await _seed_candidates(4)

    left, right = await asyncio.gather(
        reembed_module._reserve(limit=2, tenant_id=tenant_id),
        reembed_module._reserve(limit=2, tenant_id=tenant_id),
    )

    left_ids = {document_id for _tenant, document_id in left}
    right_ids = {document_id for _tenant, document_id in right}
    assert left_ids.isdisjoint(right_ids)
    assert left_ids | right_ids == set(document_ids)

    async with db_session.session_scope() as session:
        for document_id in document_ids:
            document = await DocumentRepository(session, tenant_id).get(document_id)
            assert document is not None
            assert document.status is DocumentStatus.PENDING


async def test_partial_broker_publish_releases_failures_and_rerun_resumes(
    operator_db: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tenant_id, document_ids = await _seed_candidates(3)
    reserved_order = sorted(document_ids)

    async def _preflight() -> None:
        return None

    async def _no_dispose() -> None:
        return None

    outcomes = iter((True, False, False))
    monkeypatch.setattr(reembed_module, "_preflight", _preflight)
    monkeypatch.setattr(reembed_module, "dispose_engine", _no_dispose)
    monkeypatch.setattr(reembed_module, "enqueue_ingestion", lambda *_args: next(outcomes))

    with pytest.raises(SystemExit) as excinfo:
        await reembed_module._main(["--execute", "--limit", "3"])
    assert excinfo.value.code == 1

    # The accepted message owns its Pending reservation. Definite publish
    # failures were released and are exactly what a rerun previews.
    async with db_session.session_scope() as session:
        states: dict[UUID, DocumentStatus] = {}
        for document_id in document_ids:
            document = await DocumentRepository(session, tenant_id).get(document_id)
            assert document is not None
            states[document_id] = document.status
    assert states[reserved_order[0]] is DocumentStatus.PENDING
    assert states[reserved_order[1]] is DocumentStatus.READY
    assert states[reserved_order[2]] is DocumentStatus.READY

    rerun = await reembed_module._candidates(limit=20, tenant_id=tenant_id)
    assert [document_id for _tenant, document_id in rerun] == reserved_order[1:]

    output = capsys.readouterr().out
    assert output.count('"outcome": "published"') == 1
    assert output.count('"outcome": "broker_failed_released"') == 2
    assert "reserved: 3; published: 1; deferred: 2" in output
