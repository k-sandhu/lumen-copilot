"""Refresh-token + user-lookup repository tests (issue #19, spec 0004 §2.3).

Offline (in-memory async SQLite, mirroring ``test_db_repositories``). They pin:

* ``RefreshTokenRepository`` round-trips, revokes (idempotently), and is
  **tenant-scoped** (INV-1 — a tenant-B repo cannot see/revoke a tenant-A token);
* ``UserLookupRepository`` is the one deliberate non-tenant-scoped lookup
  (email → user, hash → token-owner) the pre-identity login/refresh step needs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import (
    RefreshTokenRepository,
    UserLookupRepository,
    UserRepository,
)
from app.domain.entities import Role

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


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
    from app.db.repositories import TenantRepository

    tenants = TenantRepository(session)
    a = await tenants.create(name="Tenant A")
    b = await tenants.create(name="Tenant B")
    return a.id, b.id


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


async def test_refresh_token_round_trips(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    repo = RefreshTokenRepository(session, tenant_a)
    created = await repo.create(user_id=user.id, token_hash="hash-1", expires_at=_future())
    assert created.tenant_id == tenant_a
    assert created.revoked_at is None

    fetched = await repo.get_by_hash("hash-1")
    assert fetched is not None and fetched.id == created.id


async def test_refresh_token_revoke_is_idempotent(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    repo = RefreshTokenRepository(session, tenant_a)
    await repo.create(user_id=user.id, token_hash="hash-1", expires_at=_future())

    assert await repo.revoke("hash-1") is True
    revoked = await repo.get_by_hash("hash-1")
    assert revoked is not None and revoked.revoked_at is not None
    # A second revoke is a no-op success.
    assert await repo.revoke("hash-1") is True
    # Revoking an unknown hash returns False.
    assert await repo.revoke("nope") is False


async def test_inv1_refresh_token_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    await RefreshTokenRepository(session, tenant_a).create(
        user_id=user.id, token_hash="hash-a", expires_at=_future()
    )
    repo_b = RefreshTokenRepository(session, tenant_b)
    # Tenant B cannot see or revoke tenant A's token (INV-1, fail closed).
    assert await repo_b.get_by_hash("hash-a") is None
    assert await repo_b.revoke("hash-a") is False
    # ...and the token is still live for its owning tenant.
    still = await RefreshTokenRepository(session, tenant_a).get_by_hash("hash-a")
    assert still is not None and still.revoked_at is None


async def test_user_lookup_finds_by_email_and_token_hash(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="dev@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    await RefreshTokenRepository(session, tenant_a).create(
        user_id=user.id, token_hash="rt-hash", expires_at=_future()
    )

    lookup = UserLookupRepository(session)
    found = await lookup.find_by_email("dev@acme.test")
    assert found is not None and found.id == user.id
    assert await lookup.find_by_email("ghost@acme.test") is None

    owner = await lookup.find_refresh_token_owner("rt-hash")
    assert owner is not None and owner.user_id == user.id and owner.tenant_id == tenant_a
    assert await lookup.find_refresh_token_owner("missing") is None


def test_refresh_token_repository_requires_tenant_scope() -> None:
    """INV-1: cannot build the scoped repo without a tenant id."""
    with pytest.raises(TypeError):
        RefreshTokenRepository(session=None)  # type: ignore[call-arg]
