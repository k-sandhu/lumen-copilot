"""Artifact retention janitor — the purge core (issue #208 §6, the stub).

Retention is *optional v1* (#208 §6): the janitor is **not scheduled** yet
(that's E-Sched). But the purge core it will call must be correct and testable, so
this exercises ``purge_expired_artifacts_async`` on in-memory SQLite (offline):

* it purges only rows whose ``retention_expires_at`` has elapsed — removing the
  storage object **and** the row — and leaves keep-forever / not-yet-expired rows;
* it is tenant-scoped (INV-1) and idempotent (a re-run over an already-purged set
  removes nothing).

The task runs its DB work through ``tenant_session_scope`` (the module-global
sessionmaker), so — like ``test_ingestion_task`` — we point ``db.session`` at a
SQLite engine for the test; ``bind_tenant`` no-ops off Postgres. The object store
is a fake that records deletions.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.db.base import Base
from app.db.repositories import ArtifactRepository, TenantRepository, UserRepository
from app.domain.entities import ArtifactProducedBy, Role
from app.tasks.artifact_retention import purge_expired_artifacts_async

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


class _FakeStore:
    """Records artifact deletions; the purge core only calls ``delete_artifact``."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete_artifact(self, tenant_id: str, key: str) -> None:
        self.deleted.append((tenant_id, key))


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


async def _make_tenant() -> uuid.UUID:
    """Provision a tenant and return its id (its own transaction)."""
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="T")
        return tenant.id


async def _seed_artifact(
    tenant_id: uuid.UUID,
    *,
    filename: str,
    retention_expires_at: datetime | None,
) -> tuple[uuid.UUID, str]:
    """Create a fresh user + an artifact in ``tenant_id`` (its own transaction)."""
    async with db_session.session_scope() as session:
        user = await UserRepository(session, tenant_id).create(
            email=f"u-{uuid.uuid4()}@x.test", password_hash="h", roles=[Role.MEMBER]
        )
        art = await ArtifactRepository(session, tenant_id).create(
            owner_id=user.id,
            produced_by=ArtifactProducedBy.TOOL,
            filename=filename,
            mime_type="text/csv",
            size_bytes=3,
            storage_key=f"artifacts/{tenant_id}/{filename}",
            sha256="deadbeef",
            retention_expires_at=retention_expires_at,
        )
        return art.id, art.storage_key


async def test_purge_removes_only_expired_rows_and_objects(sqlite_engine: None) -> None:
    tenant = await _make_tenant()
    now = datetime.now(UTC)
    keep_id, _ = await _seed_artifact(tenant, filename="keep.csv", retention_expires_at=None)
    future_id, _ = await _seed_artifact(
        tenant, filename="future.csv", retention_expires_at=now + timedelta(days=1)
    )
    past_id, past_key = await _seed_artifact(
        tenant, filename="past.csv", retention_expires_at=now - timedelta(days=1)
    )

    store = _FakeStore()
    result = await purge_expired_artifacts_async(tenant, now=now, object_store=store)  # type: ignore[arg-type]

    assert result.purged == 1
    assert result.scanned == 1
    # Only the past object was deleted from storage.
    assert store.deleted == [(str(tenant), past_key)]

    # The expired row is gone; the keep/future rows remain.
    async with db_session.session_scope() as session:
        repo = ArtifactRepository(session, tenant)
        assert await repo.get(past_id) is None
        assert await repo.get(keep_id) is not None
        assert await repo.get(future_id) is not None


async def test_purge_is_idempotent(sqlite_engine: None) -> None:
    tenant = await _make_tenant()
    now = datetime.now(UTC)
    await _seed_artifact(tenant, filename="past.csv", retention_expires_at=now - timedelta(days=1))

    store = _FakeStore()
    first = await purge_expired_artifacts_async(tenant, now=now, object_store=store)  # type: ignore[arg-type]
    assert first.purged == 1
    # A second sweep finds nothing left to purge.
    second = await purge_expired_artifacts_async(tenant, now=now, object_store=store)  # type: ignore[arg-type]
    assert second.purged == 0
    assert second.scanned == 0


async def test_purge_is_tenant_scoped(sqlite_engine: None) -> None:
    tenant_a = await _make_tenant()
    tenant_b = await _make_tenant()
    now = datetime.now(UTC)
    await _seed_artifact(
        tenant_a, filename="a-past.csv", retention_expires_at=now - timedelta(days=1)
    )

    store = _FakeStore()
    # Sweeping tenant B purges nothing (A's expired row is invisible to B).
    result = await purge_expired_artifacts_async(tenant_b, now=now, object_store=store)  # type: ignore[arg-type]
    assert result.purged == 0
    assert store.deleted == []
