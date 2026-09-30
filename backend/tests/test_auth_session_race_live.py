"""Postgres row-lock proof for slot-aware refresh/logout ordering (#580).

Offline-safe by default. Opt in against a disposable Postgres database with:
``RUN_LIVE=1 AUTH_RACE_DATABASE_URL=postgresql+asyncpg://... pytest ...``.
The test creates and drops only its three tables; never point it at app data.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Table, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import (
    InvalidTokenError,
    Principal,
    RefreshSupersededError,
    hash_password,
    hash_refresh_token,
)
from app.core.config import Settings
from app.db.base import Base
from app.db.models import AuditEvent, RefreshToken, Tenant, User
from app.db.repositories import RefreshTokenRepository, UserRepository
from app.domain.entities import User as UserEntity
from app.services.auth_service import AuthService, AuthSlotCollisionError, IssuedTokens

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


@dataclass(frozen=True)
class _LegacyRace:
    factory: async_sessionmaker[AsyncSession]
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    secret: str
    email: str
    password: str
    settings: Settings


@pytest.fixture
async def legacy_auth_race() -> AsyncIterator[_LegacyRace]:
    assert _DATABASE_URL is not None
    engine = create_async_engine(_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    race = _LegacyRace(
        factory=factory,
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        secret="legacy-refresh-before-rotation",
        email="legacy-race@example.test",
        password="legacy-race-password",
        settings=Settings(AUTH_SESSION_MAX_ACTIVE=2),
    )
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
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_AUTH_TABLES)
        )
    try:
        async with factory() as seed:
            seed.add(Tenant(id=race.tenant_id, name="Legacy refresh race"))
            await seed.flush()
            seed.add(
                User(
                    id=race.user_id,
                    tenant_id=race.tenant_id,
                    email=race.email,
                    password_hash=hash_password(race.password),
                    roles=["member"],
                )
            )
            await seed.flush()
            seed.add(
                RefreshToken(
                    id=race.session_id,
                    tenant_id=race.tenant_id,
                    user_id=race.user_id,
                    token_hash=hash_refresh_token(race.secret),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await seed.commit()
        yield race
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.drop_all(
                    sync_connection, tables=list(reversed(_AUTH_TABLES))
                )
            )
        await engine.dispose()


async def _backend_pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


async def _wait_until_blocked[T](
    factory: async_sessionmaker[AsyncSession],
    task: asyncio.Task[T],
    *,
    waiting_pid: int,
    blocking_pid: int,
) -> str:
    """Observe the actual PostgreSQL blocker, without elapsed-time guesses."""
    async with factory() as observer:
        while not task.done():
            row = (
                await observer.execute(
                    text(
                        "SELECT query FROM pg_stat_activity "
                        "WHERE pid = :waiting_pid "
                        "AND :blocking_pid = ANY(pg_blocking_pids(pid))"
                    ),
                    {"waiting_pid": waiting_pid, "blocking_pid": blocking_pid},
                )
            ).one_or_none()
            if row is not None:
                return str(row.query)
    # Propagate an unexpected production error rather than spinning forever.
    await task
    pytest.fail("Concurrent operation completed without waiting for the held database lock")


async def _admit_session(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    selected_id: uuid.UUID,
    new_id: uuid.UUID,
    presented_ids: frozenset[uuid.UUID],
    expires_at: datetime,
) -> tuple[uuid.UUID, ...]:
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
@pytest.mark.parametrize(
    "operation", ["legacy-refresh", "slot-refresh", "legacy-logout", "slot-logout"]
)
async def test_login_refresh_and_logout_lock_user_before_any_token_row(
    legacy_auth_race: _LegacyRace, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Actual auth services share one global lock order (R4-004)."""
    race = legacy_auth_race
    user_locked = asyncio.Event()
    release_login = asyncio.Event()
    refresh_lock_attempted = asyncio.Event()
    first_refresh_lock: list[str] = []
    original_lock = UserRepository.lock_for_auth_session_admission

    async with race.factory() as login_session, race.factory() as refresh_session:
        login_pid = await _backend_pid(login_session)
        refresh_pid = await _backend_pid(refresh_session)
        connection = await refresh_session.connection()
        connection_info = connection.sync_connection.info
        connection_info["observe_refresh_order"] = True
        sync_engine = connection.sync_connection.engine

        async def held_login_lock(
            repository: UserRepository, user_id: uuid.UUID
        ) -> UserEntity | None:
            user = await original_lock(repository, user_id)
            if repository._session is login_session:
                user_locked.set()
                await release_login.wait()
            return user

        def observe_lock(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            if (
                conn.info.get("observe_refresh_order")
                and "FOR UPDATE" in statement
                and not first_refresh_lock
            ):
                first_refresh_lock.append(statement)
                refresh_lock_attempted.set()

        monkeypatch.setattr(UserRepository, "lock_for_auth_session_admission", held_login_lock)
        event.listen(sync_engine, "before_cursor_execute", observe_lock)
        login_task = asyncio.create_task(
            AuthService(login_session, race.settings).login(
                email=race.email,
                password=race.password,
                session_id=uuid.uuid4(),
                request_id="login-refresh-order",
                source_ip="127.0.0.1",
            )
        )
        refresh_task: asyncio.Task[IssuedTokens] | asyncio.Task[bool] | None = None
        try:
            await user_locked.wait()
            service = AuthService(refresh_session, race.settings)
            session_id = race.session_id if operation.startswith("slot") else None
            if operation.endswith("refresh"):
                refresh_task = asyncio.create_task(
                    service.refresh(raw_refresh_token=race.secret, session_id=session_id)
                )
            else:
                refresh_task = asyncio.create_task(
                    service.logout(
                        Principal(user_id=race.user_id, tenant_id=race.tenant_id, roles=()),
                        raw_refresh_token=race.secret,
                        session_id=session_id,
                        request_id="login-logout-order",
                        source_ip="127.0.0.1",
                    )
                )
            await refresh_lock_attempted.wait()
            # Assert before releasing login: no token lock may be held while
            # refresh waits for the same user login already owns.
            assert "FROM users" in first_refresh_lock[0], first_refresh_lock[0]
            blocking_query = await _wait_until_blocked(
                race.factory,
                refresh_task,
                waiting_pid=refresh_pid,
                blocking_pid=login_pid,
            )
            assert "FROM users" in blocking_query
            release_login.set()
            await login_task
            await login_session.commit()
            tokens = await refresh_task
            await refresh_session.commit()
            if isinstance(tokens, IssuedTokens):
                assert tokens.refresh_token != race.secret
            else:
                assert tokens is True
        finally:
            if refresh_task is not None and not refresh_task.done():
                refresh_task.cancel()
                await asyncio.gather(refresh_task, return_exceptions=True)
            release_login.set()
            await asyncio.gather(login_task, return_exceptions=True)
            event.remove(sync_engine, "before_cursor_execute", observe_lock)
            connection_info.pop("observe_refresh_order", None)

    async with race.factory() as verify:
        rows = (await verify.execute(select(RefreshToken))).scalars().all()
        assert len(rows) == 2  # Login inserted one; refresh/logout inserted none.
        family = next(row for row in rows if row.id == race.session_id)
        if isinstance(tokens, IssuedTokens):
            assert family.revoked_at is None
            assert family.token_hash == hash_refresh_token(tokens.refresh_token)
        else:
            assert family.revoked_at is not None


@_live
@pytest.mark.live
async def test_concurrent_legacy_refresh_revalidates_after_user_lock_and_preserves_family(
    legacy_auth_race: _LegacyRace,
) -> None:
    """One legacy secret produces one winner and keeps one stable row (R4-004)."""
    race = legacy_auth_race
    async with race.factory() as winner, race.factory() as loser:
        winner_pid = await _backend_pid(winner)
        loser_pid = await _backend_pid(loser)
        winning_tokens = await AuthService(winner, race.settings).refresh(
            raw_refresh_token=race.secret,
        )
        loser_task = asyncio.create_task(
            AuthService(loser, race.settings).refresh(raw_refresh_token=race.secret)
        )
        try:
            blocked_query = await _wait_until_blocked(
                race.factory, loser_task, waiting_pid=loser_pid, blocking_pid=winner_pid
            )
            assert "FROM users" in blocked_query, blocked_query
            await winner.commit()
            with pytest.raises(InvalidTokenError):
                await loser_task
            await loser.rollback()
        finally:
            if not loser_task.done():
                loser_task.cancel()
                await asyncio.gather(loser_task, return_exceptions=True)

    async with race.factory() as verify:
        rows = (await verify.execute(select(RefreshToken))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == race.session_id
        assert rows[0].revoked_at is None
        assert rows[0].token_hash == hash_refresh_token(winning_tokens.refresh_token)
        next_tokens = await AuthService(verify, race.settings).refresh(
            raw_refresh_token=winning_tokens.refresh_token,
        )
        assert next_tokens.refresh_token != winning_tokens.refresh_token
        await verify.commit()


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
            refresh_pid = await _backend_pid(refresh_session)
            logout_pid = await _backend_pid(logout_session)
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
            await _wait_until_blocked(
                factory, logout_task, waiting_pid=logout_pid, blocking_pid=refresh_pid
            )
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
            logout_pid = await _backend_pid(logout_session)
            refresh_pid = await _backend_pid(refresh_session)
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
            await _wait_until_blocked(
                factory, refresh_task, waiting_pid=refresh_pid, blocking_pid=logout_pid
            )
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
        async with factory() as first, factory() as second:
            first_pid = await _backend_pid(first)
            second_pid = await _backend_pid(second)
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
                    second,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    selected_id=selected_id,
                    new_id=second_new,
                    presented_ids=frozenset({selected_id, first_new}),
                    expires_at=expires_at,
                )
            )
            await _wait_until_blocked(
                factory, second_task, waiting_pid=second_pid, blocking_pid=first_pid
            )
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
    old_secret = "same-slot-refresh-before-rotation"
    settings = Settings()

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
                    token_hash=hash_refresh_token(old_secret),
                    expires_at=expires_at,
                )
            )
            await seed.commit()

        async with factory() as winner, factory() as loser:
            winner_pid = await _backend_pid(winner)
            loser_pid = await _backend_pid(loser)
            winning_tokens = await AuthService(winner, settings).refresh(
                raw_refresh_token=old_secret,
                session_id=session_id,
            )
            loser_task = asyncio.create_task(
                AuthService(loser, settings).refresh(
                    raw_refresh_token=old_secret,
                    session_id=session_id,
                )
            )
            await _wait_until_blocked(
                factory, loser_task, waiting_pid=loser_pid, blocking_pid=winner_pid
            )
            await winner.commit()
            # This calls the production lookup and typed service error. An
            # obsolete-hash SQL predicate would lose the family after commit
            # and raise InvalidTokenError instead of non-destructive supersession.
            with pytest.raises(RefreshSupersededError):
                await loser_task
            await loser.rollback()

        async with factory() as verify:
            row = (
                await verify.execute(select(RefreshToken).where(RefreshToken.id == session_id))
            ).scalar_one()
            assert row.token_hash == hash_refresh_token(winning_tokens.refresh_token)
            assert row.revoked_at is None
            next_tokens = await AuthService(verify, settings).refresh(
                raw_refresh_token=winning_tokens.refresh_token,
                session_id=session_id,
            )
            assert next_tokens.refresh_token != winning_tokens.refresh_token
            await verify.commit()
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
