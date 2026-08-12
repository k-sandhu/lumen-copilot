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
from sqlalchemy import Table, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import hash_password
from app.core.config import Settings
from app.db.base import Base
from app.db.models import AuditEvent, RefreshToken, Tenant, User
from app.db.repositories import RefreshTokenRepository, UserRepository
from app.services.auth_service import AuthService, AuthSlotCollisionError

_DATABASE_URL = os.environ.get("AUTH_RACE_DATABASE_URL")
_live = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1" or not _DATABASE_URL,
    reason="requires RUN_LIVE=1 and a disposable AUTH_RACE_DATABASE_URL",
)

_AUTH_TABLES: list[Table] = [
    Tenant.__table__,  # type: ignore[list-item]
    User.__table__,  # type: ignore[list-item]
    RefreshToken.__table__,  # type: ignore[list-item]
    AuditEvent.__table__,  # type: ignore[list-item]
]


async def _wait_until_blocked(task: asyncio.Task[bool]) -> None:
    """Prove the second transaction has not passed the first row lock."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.2)


async def _locked_slot_hash(
    factory: async_sessionmaker[AsyncSession], session_id: uuid.UUID
) -> str | None:
    """Lock by routing id, then return the current hash for service-side compare."""
    async with factory() as session:
        row = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        return row.token_hash if row is not None else None


async def _admit_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    selected_id: uuid.UUID,
    new_id: uuid.UUID,
    presented_ids: frozenset[uuid.UUID],
    expires_at: datetime,
) -> tuple[uuid.UUID, ...]:
    async with factory() as session:
        locked = await UserRepository(session, tenant_id).lock_for_auth_session_admission(user_id)
        assert locked is not None
        repository = RefreshTokenRepository(session, tenant_id)
        await repository.create(
            user_id=user_id,
            token_hash=new_id.hex * 2,
            expires_at=expires_at,
            token_id=new_id,
        )
        cleanup = await repository.enforce_active_session_cap(
            user_id=user_id,
            max_active=2,
            preserve_ids={selected_id, new_id},
            presented_cookie_ids=presented_ids,
            cleanup_limit=8,
        )
        await session.commit()
        return cleanup


async def _login_exact_slot(
    factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    email: str,
    password: str,
    session_id: uuid.UUID,
    request_id: str,
) -> tuple[str, str | None, str | None]:
    """Mirror the router boundary: collision commits only its denied audit."""
    async with factory() as session:
        try:
            tokens = await AuthService(session, settings).login(
                email=email,
                password=password,
                session_id=session_id,
                request_id=request_id,
                source_ip="127.0.0.1",
            )
        except AuthSlotCollisionError:
            await session.commit()
            return ("collision", None, None)
        await session.commit()
        return ("success", tokens.access.token, tokens.refresh_token)


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
        privilege = (
            await connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert tuple(privilege) == (False, False, False, False)
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=_AUTH_TABLES,
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
                    tables=list(reversed(_AUTH_TABLES)),
                )
            )
        await engine.dispose()


@_live
@pytest.mark.live
async def test_concurrent_session_admission_is_serialized_and_never_exceeds_cap() -> None:
    """The tenant/user lock prevents a two-login phantom overshoot (R3-003)."""
    assert _DATABASE_URL is not None
    engine = create_async_engine(_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    selected_id = uuid.uuid4()
    first_new = uuid.uuid4()
    second_new = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(days=1)

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=_AUTH_TABLES,
            )
        )
    try:
        async with factory() as seed:
            seed.add(Tenant(id=tenant_id, name="Admission race"))
            await seed.flush()
            seed.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email="admission@example.test",
                    password_hash="unused",
                    roles=["member"],
                )
            )
            await seed.flush()
            seed.add(
                RefreshToken(
                    id=selected_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    token_hash="1" * 64,
                    expires_at=expires_at,
                )
            )
            await seed.commit()

        # Hold the serialized admission boundary after T1 inserted. T2 must not
        # count the same pre-insert set and independently admit above the cap.
        async with factory() as first:
            locked = await UserRepository(first, tenant_id).lock_for_auth_session_admission(user_id)
            assert locked is not None
            first_repo = RefreshTokenRepository(first, tenant_id)
            await first_repo.create(
                user_id=user_id,
                token_hash="2" * 64,
                expires_at=expires_at,
                token_id=first_new,
            )
            await first_repo.enforce_active_session_cap(
                user_id=user_id,
                max_active=2,
                preserve_ids={selected_id, first_new},
                presented_cookie_ids=frozenset({selected_id, first_new}),
                cleanup_limit=8,
            )
            second_task = asyncio.create_task(
                _admit_session(
                    factory,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    selected_id=selected_id,
                    new_id=second_new,
                    presented_ids=frozenset({selected_id, first_new}),
                    expires_at=expires_at,
                )
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second_task), timeout=0.2)
            await first.commit()
            assert await second_task == (first_new,)

        async with factory() as verify:
            rows = (
                (
                    await verify.execute(
                        select(RefreshToken)
                        .where(RefreshToken.tenant_id == tenant_id)
                        .order_by(RefreshToken.created_at, RefreshToken.id)
                    )
                )
                .scalars()
                .all()
            )
            active_ids = {row.id for row in rows if row.revoked_at is None}
            assert active_ids == {selected_id, second_new}
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.drop_all(
                    sync_connection,
                    tables=list(reversed(_AUTH_TABLES)),
                )
            )
        await engine.dispose()


@_live
@pytest.mark.live
async def test_concurrent_same_slot_loser_observes_winner_without_revoking_it() -> None:
    """READ COMMITTED re-checks the ID-locked row after winner commit (R3-001)."""
    assert _DATABASE_URL is not None
    engine = create_async_engine(_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(days=1)
    old_hash = "e" * 64
    winning_hash = "f" * 64

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=_AUTH_TABLES,
            )
        )
    try:
        async with factory() as seed:
            seed.add(Tenant(id=tenant_id, name="Same-slot race"))
            await seed.flush()
            seed.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email="same-slot@example.test",
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
                    token_hash=old_hash,
                    expires_at=expires_at,
                )
            )
            await seed.commit()

        async with factory() as winner:
            rotated = await RefreshTokenRepository(winner, tenant_id).rotate_session(
                session_id,
                user_id=user_id,
                expected_hash=old_hash,
                new_hash=winning_hash,
                expires_at=expires_at,
            )
            assert rotated is True
            loser_task = asyncio.create_task(_locked_slot_hash(factory, session_id))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(loser_task), timeout=0.2)
            await winner.commit()
            # The old implementation's id+old-hash predicate became no row here,
            # which led the client to destroy the shared winner. ID-first lookup
            # instead returns the winner's hash for a safe mismatch 401.
            assert await loser_task == winning_hash

        async with factory() as verify:
            row = (
                await verify.execute(select(RefreshToken).where(RefreshToken.id == session_id))
            ).scalar_one()
            assert row.token_hash == winning_hash
            assert row.revoked_at is None
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.drop_all(
                    sync_connection,
                    tables=list(reversed(_AUTH_TABLES)),
                )
            )
        await engine.dispose()


@_live
@pytest.mark.live
async def test_concurrent_exact_slot_collision_commits_one_sanitized_denial() -> None:
    """Two verified logins race one UUID: one session and one denied audit (R3-005)."""
    assert _DATABASE_URL is not None
    engine = create_async_engine(_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    email = "collision-race@example.test"
    password = "collision-race-password"
    settings = Settings(AUTH_SESSION_MAX_ACTIVE=2)

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=_AUTH_TABLES,
            )
        )
    try:
        async with factory() as seed:
            seed.add(Tenant(id=tenant_id, name="Collision race"))
            await seed.flush()
            seed.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=email,
                    password_hash=hash_password(password),
                    roles=["member"],
                )
            )
            await seed.commit()

        results = await asyncio.gather(
            _login_exact_slot(
                factory,
                settings=settings,
                email=email,
                password=password,
                session_id=session_id,
                request_id="collision-race-a",
            ),
            _login_exact_slot(
                factory,
                settings=settings,
                email=email,
                password=password,
                session_id=session_id,
                request_id="collision-race-b",
            ),
        )
        assert sorted(result[0] for result in results) == ["collision", "success"]
        winner = next(result for result in results if result[0] == "success")

        async with factory() as verify:
            tokens = (
                (await verify.execute(select(RefreshToken).where(RefreshToken.id == session_id)))
                .scalars()
                .all()
            )
            audits = (
                (
                    await verify.execute(
                        select(AuditEvent)
                        .where(AuditEvent.tenant_id == tenant_id)
                        .order_by(AuditEvent.ts, AuditEvent.id)
                    )
                )
                .scalars()
                .all()
            )
        collisions = [
            event
            for event in audits
            if event.action == "auth.login_failed"
            and event.event_metadata == {"reason": "slot_collision"}
        ]
        assert len(tokens) == 1
        assert len(collisions) == 1
        assert collisions[0].outcome == "denied"
        assert collisions[0].actor_id == user_id
        assert len([event for event in audits if event.action == "auth.login"]) == 1
        serialized = repr(collisions[0].event_metadata)
        for secret in (email, password, str(session_id), winner[1], winner[2]):
            assert secret is not None and secret not in serialized
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.drop_all(
                    sync_connection,
                    tables=list(reversed(_AUTH_TABLES)),
                )
            )
        await engine.dispose()
