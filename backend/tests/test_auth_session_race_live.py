"""Postgres row-lock proof for slot-aware refresh/logout ordering (#580).

Offline-safe by default. Opt in against a disposable Postgres database with:
``RUN_LIVE=1 AUTH_RACE_DATABASE_URL=postgresql+asyncpg://... pytest ...``.
The test creates and drops only its three tables; never point it at app data.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import RefreshToken, Tenant, User
from app.db.repositories import RefreshTokenRepository

_DATABASE_URL = os.environ.get("AUTH_RACE_DATABASE_URL")
_live = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1" or not _DATABASE_URL,
    reason="requires RUN_LIVE=1 and a disposable AUTH_RACE_DATABASE_URL",
)


async def _wait_until_blocked(task: asyncio.Task[bool]) -> None:
    """Prove the second transaction has not passed the first row lock."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)


@_live
@pytest.mark.live
async def test_refresh_and_logout_commit_order_serializes_one_session_row() -> None:
    assert _DATABASE_URL is not None
    engine = create_async_engine(_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(days=1)

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Tenant.__table__, User.__table__, RefreshToken.__table__],
            )
        )
    try:
        async with factory() as seed:
            seed.add(Tenant(id=tenant_id, name="Auth race"))
            await seed.flush()
            seed.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email="race@example.test",
                    password_hash="unused",
                    roles=["member"],
                )
            )
            await seed.flush()
            seed.add(
                RefreshToken(
                    id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    token_hash="a" * 64,
                    expires_at=expires_at,
                )
            )
            await seed.commit()

        # Refresh locks/rotates first; logout waits, then revokes the NEW hash.
        async with factory() as refresh_session, factory() as logout_session:
            refreshed = await RefreshTokenRepository(refresh_session, tenant_id).rotate_session(
                session_id,
                user_id=user_id,
                expected_hash="a" * 64,
                new_hash="b" * 64,
                expires_at=expires_at,
            )
            assert refreshed is True
            logout_task = asyncio.create_task(
                RefreshTokenRepository(logout_session, tenant_id).revoke_session(
                    session_id, user_id=user_id
                )
            )
            await _wait_until_blocked(logout_task)
            await refresh_session.commit()
            assert await logout_task is True
            await logout_session.commit()

        async with factory() as verify:
            row = (
                await verify.execute(select(RefreshToken).where(RefreshToken.id == session_id))
            ).scalar_one()
            assert row.token_hash == "b" * 64
            assert row.revoked_at is not None

        # Logout locks/revokes first; refresh waits, then its CAS fails closed.
        replacement_id = uuid.uuid4()
        async with factory() as seed:
            seed.add(
                RefreshToken(
                    id=replacement_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    token_hash="c" * 64,
                    expires_at=expires_at,
                )
            )
            await seed.commit()

        async with factory() as logout_session, factory() as refresh_session:
            revoked = await RefreshTokenRepository(logout_session, tenant_id).revoke_session(
                replacement_id, user_id=user_id
            )
            assert revoked is True
            refresh_task = asyncio.create_task(
                RefreshTokenRepository(refresh_session, tenant_id).rotate_session(
                    replacement_id,
                    user_id=user_id,
                    expected_hash="c" * 64,
                    new_hash="d" * 64,
                    expires_at=expires_at,
                )
            )
            await _wait_until_blocked(refresh_task)
            await logout_session.commit()
            assert await refresh_task is False
            await refresh_session.commit()
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.drop_all(
                    sync_connection,
                    tables=[RefreshToken.__table__, User.__table__, Tenant.__table__],
                )
            )
        await engine.dispose()
