"""ArtifactRepository unit tests — tenancy isolation + retention sweep (#208).

The data-layer invariants for the artifact store, on in-memory SQLite (offline;
the real ``vector``/RLS shapes are pinned by the migration test). Headlines:

* **INV-1** — a repository bound to tenant A never reads tenant B's artifacts: a
  cross-tenant ``get`` returns ``None`` and B's list excludes A's rows.
* **owner scoping** — ``list_for_owner_page`` returns only the given owner's rows.
* **retention** — ``list_expired`` returns only rows whose ``retention_expires_at``
  has elapsed (NULL-retention rows are never swept), oldest-expiry first.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import ArtifactRepository, TenantRepository, UserRepository
from app.domain.entities import ArtifactProducedBy, Role

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def two_tenants(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    tenants = TenantRepository(session)
    a = await tenants.create(name="Tenant A")
    b = await tenants.create(name="Tenant B")
    return a.id, b.id


async def _make_user(session: AsyncSession, tenant_id: uuid.UUID, email: str) -> uuid.UUID:
    user = await UserRepository(session, tenant_id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    return user.id


async def _make_artifact(
    repo: ArtifactRepository,
    *,
    owner_id: uuid.UUID,
    filename: str = "a.csv",
    retention_expires_at: datetime | None = None,
    produced_by: ArtifactProducedBy = ArtifactProducedBy.TOOL,
) -> uuid.UUID:
    data = filename.encode()
    art = await repo.create(
        owner_id=owner_id,
        produced_by=produced_by,
        filename=filename,
        mime_type="text/csv",
        size_bytes=len(data),
        storage_key=f"artifacts/x/{filename}",
        sha256="deadbeef",
        retention_expires_at=retention_expires_at,
    )
    return art.id


# --- Happy path -------------------------------------------------------------


async def test_create_and_get_round_trips(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    repo = ArtifactRepository(session, tenant_a)
    art_id = await _make_artifact(repo, owner_id=user_a, filename="report.csv")

    fetched = await repo.get(art_id)
    assert fetched is not None
    assert fetched.tenant_id == tenant_a
    assert fetched.owner_id == user_a
    assert fetched.produced_by is ArtifactProducedBy.TOOL
    assert fetched.filename == "report.csv"


# --- INV-1: tenant isolation ------------------------------------------------


async def test_get_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A tenant-B repository cannot resolve a tenant-A artifact id (→ None → 404)."""
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    art_id = await _make_artifact(ArtifactRepository(session, tenant_a), owner_id=user_a)

    assert await ArtifactRepository(session, tenant_b).get(art_id) is None
    # Delete from the wrong tenant is also a no-op (False), and the row survives.
    assert await ArtifactRepository(session, tenant_b).delete(art_id) is False
    assert await ArtifactRepository(session, tenant_a).get(art_id) is not None


async def test_list_for_owner_is_owner_and_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_a2 = await _make_user(session, tenant_a, "a2@x.test")
    user_b = await _make_user(session, tenant_b, "b@x.test")

    repo_a = ArtifactRepository(session, tenant_a)
    await _make_artifact(repo_a, owner_id=user_a, filename="a1.csv")
    await _make_artifact(repo_a, owner_id=user_a2, filename="other-owner.csv")
    await _make_artifact(ArtifactRepository(session, tenant_b), owner_id=user_b, filename="b1.csv")

    mine = await repo_a.list_for_owner_page(user_a, limit=10)
    assert [a.filename for a in mine] == ["a1.csv"]  # not the other owner's, not B's


# --- Retention sweep --------------------------------------------------------


async def test_list_expired_returns_only_elapsed_non_null(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    repo = ArtifactRepository(session, tenant_a)
    now = datetime.now(UTC)

    keep = await _make_artifact(
        repo, owner_id=user_a, filename="keep.csv", retention_expires_at=None
    )
    future = await _make_artifact(
        repo, owner_id=user_a, filename="future.csv", retention_expires_at=now + timedelta(days=1)
    )
    past = await _make_artifact(
        repo, owner_id=user_a, filename="past.csv", retention_expires_at=now - timedelta(days=1)
    )

    expired = await repo.list_expired(now=now)
    expired_ids = {a.id for a in expired}
    assert past in expired_ids
    assert keep not in expired_ids  # NULL retention = keep forever
    assert future not in expired_ids  # not yet elapsed


async def test_list_expired_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    now = datetime.now(UTC)
    await _make_artifact(
        ArtifactRepository(session, tenant_a),
        owner_id=user_a,
        filename="a-expired.csv",
        retention_expires_at=now - timedelta(days=1),
    )
    # Tenant B sees none of A's expired rows.
    assert await ArtifactRepository(session, tenant_b).list_expired(now=now) == []
