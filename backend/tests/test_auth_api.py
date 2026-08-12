"""Auth API tests — the /auth contract + the required negative cases (#19).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite
database (no Postgres needed): the app's ``get_db_session`` dependency is
overridden to yield sessions from a StaticPool SQLite engine whose schema is
created from the ORM metadata. A dev user is seeded with an Argon2id hash, then:

* happy path: login → access token + httpOnly refresh cookie → /auth/me →
  refresh (rotates) → logout (revokes);
* negatives (spec 0004 §9):
  - INV-4: missing / malformed / expired bearer → 401 on /auth/me;
  - bad credentials → generic 401, no account-existence disclosure;
  - INV-8: malformed login body → 422;
  - INV-5: a role-gated route hit by the wrong role → 403 (the reusable
    ``require_roles`` seam exercised on a throwaway protected route).
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session, get_settings_dep, require_roles
from app.auth import Principal, hash_password, hashing
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.models import AuditEvent, RefreshToken, User
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app
from app.services.audit import AuditSink
from app.services.auth_service import AuthService

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_DEV_EMAIL = "dev@acme.test"
_DEV_PASSWORD = "devpassword"


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A StaticPool SQLite engine + schema; seed one tenant + dev user."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed_session:
            tenant = await TenantRepository(seed_session).create(name="Acme")
            await UserRepository(seed_session, tenant.id).create(
                email=_DEV_EMAIL,
                password_hash=hash_password(_DEV_PASSWORD),
                roles=[Role.MEMBER],
            )
            await seed_session.commit()
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> Iterator[FastAPI]:
    """The app with its DB session dependency pointed at the SQLite engine."""
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- Happy path -------------------------------------------------------------


async def test_login_returns_token_and_sets_refresh_cookie(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["access_token"]
    # The refresh token rides an httpOnly cookie, never the JSON body.
    set_cookie = resp.headers.get("set-cookie", "")
    assert "lumen_refresh_token=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    assert "path=/api/v1/auth" in set_cookie.lower()
    assert "max-age=" in set_cookie.lower()


async def test_me_returns_current_user_after_login(client: AsyncClient) -> None:
    login = await client.post(
        "/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD}
    )
    token = login.json()["access_token"]
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "id",
        "email",
        "tenant_id",
        "tenant_name",
        "roles",
        "created_at",
        "logo_url",
        "avatar_url",
    }
    assert body["email"] == _DEV_EMAIL
    # The human-readable tenant name (the tenants.name column) rides alongside the
    # raw id so the UI never has to surface the UUID (#247).
    assert body["tenant_name"] == "Acme"
    assert body["roles"] == ["member"]
    # A fresh tenant has no logo → null (the shell renders the default brand mark).
    assert body["logo_url"] is None
    # A fresh user has no avatar → null (the shell renders the initials fallback).
    assert body["avatar_url"] is None


async def test_refresh_rotates_and_old_token_is_rejected(app: FastAPI, client: AsyncClient) -> None:
    login = await client.post(
        "/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD}
    )
    original_cookie = login.cookies.get("lumen_refresh_token")
    assert original_cookie is not None

    # httpx stores the refresh cookie on the client jar; refresh uses it + rotates.
    first = await client.post("/api/v1/auth/refresh")
    assert first.status_code == 200
    assert first.json()["access_token"]

    # Replay the *original* (pre-rotation) token on a clean client (no jar) so
    # only the revoked value is presented: it must now be rejected (rotation).
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as fresh:
        replay = await fresh.post(
            "/api/v1/auth/refresh",
            cookies={"lumen_refresh_token": original_cookie},
        )
    assert replay.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient) -> None:
    login = await client.post(
        "/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD}
    )
    token = login.json()["access_token"]
    logout = await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout.status_code == 204
    # Legacy fixed-cookie compatibility never emits a shared deletion header; a
    # late response therefore cannot erase a newer slotless login's cookie.
    assert "set-cookie" not in logout.headers
    # After logout the refresh token is revoked → refresh now 401s.
    again = await client.post("/api/v1/auth/refresh")
    assert again.status_code == 401


async def test_slot_login_and_logout_emit_one_sanitized_audit_each(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Session admission/revocation remain auditable without slot secrets (R3-003)."""
    slot = "12121212-1212-4212-8212-121212121212"
    headers = {"X-Lumen-Auth-Slot": slot}
    login = await client.post(
        "/api/v1/auth/login",
        headers=headers,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    refresh_secret = login.cookies.get(f"lumen_refresh_token_{slot}")
    logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {login.json()['access_token']}", **headers},
    )
    assert login.status_code == 200
    assert logout.status_code == 204

    async with sessionmaker() as session:
        audits = (
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.action.in_(["auth.login", "auth.logout"]))
                    .order_by(AuditEvent.ts, AuditEvent.id)
                )
            )
            .scalars()
            .all()
        )
    assert sorted(event.action for event in audits) == ["auth.login", "auth.logout"]
    serialized = repr(
        [
            {
                "resource_id": event.resource_id,
                "metadata": event.event_metadata,
            }
            for event in audits
        ]
    )
    assert slot not in serialized
    assert _DEV_EMAIL not in serialized
    assert _DEV_PASSWORD not in serialized
    assert refresh_secret is not None and refresh_secret not in serialized


async def test_auth_slots_isolate_reordered_login_refresh_and_logout_cookies(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Each browser auth intent owns one cookie name; stale responses cannot overwrite it."""
    slot_a = "11111111-1111-4111-8111-111111111111"
    slot_b = "22222222-2222-4222-8222-222222222222"
    header_a = {"X-Lumen-Auth-Slot": slot_a}
    header_b = {"X-Lumen-Auth-Slot": slot_b}

    login_a = await client.post(
        "/api/v1/auth/login",
        headers=header_a,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    login_b = await client.post(
        "/api/v1/auth/login",
        headers=header_b,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    assert login_a.status_code == login_b.status_code == 200
    cookie_a = f"lumen_refresh_token_{slot_a}"
    cookie_b = f"lumen_refresh_token_{slot_b}"
    assert client.cookies.get(cookie_a) is not None
    assert client.cookies.get(cookie_b) is not None
    assert cookie_a in login_a.headers["set-cookie"]
    assert cookie_b in login_b.headers["set-cookie"]
    assert "httponly" in login_a.headers["set-cookie"].lower()
    assert "samesite=strict" in login_a.headers["set-cookie"].lower()
    async with sessionmaker() as session:
        ids = set((await session.execute(select(RefreshToken.id))).scalars().all())
    assert uuid.UUID(slot_a) in ids
    assert uuid.UUID(slot_b) in ids

    # Dynamic cookies are never scanned when routing metadata is absent.
    no_selector = await client.post("/api/v1/auth/refresh")
    assert no_selector.status_code == 401

    # Logout A revokes and expires only A's unique slot cookie. Even if this
    # response lands late, it has no cookie mutation capable of erasing B.
    logout_a = await client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {login_a.json()['access_token']}",
            **header_a,
        },
    )
    assert logout_a.status_code == 204
    deletion = logout_a.headers["set-cookie"]
    assert cookie_a in deletion
    assert "Max-Age=0" in deletion
    assert cookie_b not in deletion
    assert client.cookies.get(cookie_a) is None
    assert client.cookies.get(cookie_b) is not None

    rejected_a = await client.post("/api/v1/auth/refresh", headers=header_a)
    assert rejected_a.status_code == 401
    refreshed_b = await client.post("/api/v1/auth/refresh", headers=header_b)
    assert refreshed_b.status_code == 200
    assert refreshed_b.json()["access_token"]


async def test_slot_refresh_and_logout_serialize_on_one_session_family(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """Logout revokes the family even when it captured the pre-rotation cookie."""
    slot = "33333333-3333-4333-8333-333333333333"
    header = {"X-Lumen-Auth-Slot": slot}
    cookie_name = f"lumen_refresh_token_{slot}"
    login = await client.post(
        "/api/v1/auth/login",
        headers=header,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    old_cookie = login.cookies.get(cookie_name)
    assert old_cookie is not None

    rotated = await client.post("/api/v1/auth/refresh", headers=header)
    assert rotated.status_code == 200
    assert client.cookies.get(cookie_name) != old_cookie

    # Model a held logout request whose Cookie header was captured before the
    # refresh committed, but whose server transaction runs afterward.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as stale:
        logout = await stale.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {login.json()['access_token']}", **header},
            cookies={cookie_name: old_cookie},
        )
    assert logout.status_code == 204

    # The current rotated secret belongs to the same row/family and is revoked.
    after = await client.post("/api/v1/auth/refresh", headers=header)
    assert after.status_code == 401


async def test_losing_same_slot_refresh_does_not_revoke_the_rotated_winner(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    """An obsolete same-slot hash is a non-destructive 401 (R3-001)."""
    slot = "34343434-3434-4434-8434-343434343434"
    header = {"X-Lumen-Auth-Slot": slot}
    cookie_name = f"lumen_refresh_token_{slot}"
    login = await client.post(
        "/api/v1/auth/login",
        headers=header,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    obsolete = login.cookies.get(cookie_name)
    assert obsolete is not None

    winner = await client.post("/api/v1/auth/refresh", headers=header)
    current = client.cookies.get(cookie_name)
    assert winner.status_code == 200
    assert current is not None and current != obsolete

    # This is the database state the blocked READ COMMITTED loser observes after
    # the winner commits. It must not revoke the row or emit Delete-Cookie.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as loser:
        replay = await loser.post(
            "/api/v1/auth/refresh",
            headers=header,
            cookies={cookie_name: obsolete},
        )
    assert replay.status_code == 401
    assert replay.json()["code"] == "refresh_superseded"
    assert "set-cookie" not in replay.headers

    # The winning cookie/family is still usable and rotates normally.
    survivor = await client.post("/api/v1/auth/refresh", headers=header)
    assert survivor.status_code == 200
    assert client.cookies.get(cookie_name) not in {obsolete, current}


async def test_auth_slot_is_routing_metadata_not_refresh_authority(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    slot = "44444444-4444-4444-8444-444444444444"
    unknown_slot = "55555555-5555-4555-8555-555555555555"
    cookie_name = f"lumen_refresh_token_{slot}"
    login = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": slot},
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    raw_token = login.cookies.get(cookie_name)
    assert raw_token is not None

    # Copying the real secret under an unknown selector must still fail: the
    # selector is bound to the token row, not an alternate hash-only lookup.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as fresh:
        response = await fresh.post(
            "/api/v1/auth/refresh",
            headers={"X-Lumen-Auth-Slot": unknown_slot},
            cookies={f"lumen_refresh_token_{unknown_slot}": raw_token},
        )
    assert response.status_code == 401


async def test_logout_slot_is_bound_to_bearer_tenant_and_user(
    app: FastAPI,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    slot_a = "abababab-abab-4bab-8bab-abababababab"
    slot_b = "bcbcbcbc-bcbc-4cbc-8cbc-bcbcbcbcbcbc"
    cookie_a_name = f"lumen_refresh_token_{slot_a}"
    login_a = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": slot_a},
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    raw_a = login_a.cookies.get(cookie_a_name)
    assert raw_a is not None

    other_email = "other-tenant@example.test"
    other_password = "other-tenant-password"
    async with sessionmaker() as session:
        other_tenant = await TenantRepository(session).create(name="Other tenant")
        await UserRepository(session, other_tenant.id).create(
            email=other_email,
            password_hash=hash_password(other_password),
            roles=[Role.MEMBER],
        )
        await session.commit()
    login_b = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": slot_b},
        json={"email": other_email, "password": other_password},
    )

    # B knows A's non-secret routing id and presents A's cookie, but B's bearer
    # tenant/user cannot revoke A's family.
    mismatch = await client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {login_b.json()['access_token']}",
            "X-Lumen-Auth-Slot": slot_a,
        },
        cookies={cookie_a_name: raw_a},
    )
    assert mismatch.status_code == 204

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as persona_a:
        still_live = await persona_a.post(
            "/api/v1/auth/refresh",
            headers={"X-Lumen-Auth-Slot": slot_a},
            cookies={cookie_a_name: raw_a},
        )
    assert still_live.status_code == 200


async def test_reusing_an_active_auth_slot_fails_without_overwriting_it(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    slot = "66666666-6666-4666-8666-666666666666"
    header = {"X-Lumen-Auth-Slot": slot}
    first = await client.post(
        "/api/v1/auth/login",
        headers=header,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    original = client.cookies.get(f"lumen_refresh_token_{slot}")
    assert first.status_code == 200

    duplicate = await client.post(
        "/api/v1/auth/login",
        headers=header,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    assert duplicate.status_code == 409
    assert "set-cookie" not in duplicate.headers
    assert client.cookies.get(f"lumen_refresh_token_{slot}") == original

    async with sessionmaker() as session:
        tokens = (
            (await session.execute(select(RefreshToken).where(RefreshToken.id == uuid.UUID(slot))))
            .scalars()
            .all()
        )
        audits = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "auth.login_failed",
                        AuditEvent.event_metadata["reason"].as_string() == "slot_collision",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(tokens) == 1
    assert len(audits) == 1
    assert audits[0].outcome == "denied"
    assert audits[0].actor_id is not None
    serialized = repr(audits[0].event_metadata)
    assert _DEV_EMAIL not in serialized
    assert _DEV_PASSWORD not in serialized
    assert original not in serialized


async def test_auth_slot_collision_audit_failure_rolls_back_every_collision_write(
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit is mandatory: even a post-flush sink fault fails closed (R3-005)."""
    slot = "67676767-6767-4767-8767-676767676767"
    headers = {"X-Lumen-Auth-Slot": slot}
    first = await client.post(
        "/api/v1/auth/login",
        headers=headers,
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    assert first.status_code == 200

    real_emit = AuditSink.emit

    async def emit_then_fail(self: AuditSink, **kwargs: object) -> object:
        await real_emit(self, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("injected audit sink failure")

    monkeypatch.setattr(AuditSink, "emit", emit_then_fail)
    async with sessionmaker() as failing_session:
        with pytest.raises(RuntimeError, match="injected audit sink failure"):
            await AuthService(failing_session, get_settings()).login(
                email=_DEV_EMAIL,
                password=_DEV_PASSWORD,
                request_id="collision-audit-failure",
                source_ip="127.0.0.1",
                session_id=uuid.UUID(slot),
            )
        await failing_session.rollback()

    async with sessionmaker() as session:
        token_rows = (
            (await session.execute(select(RefreshToken).where(RefreshToken.id == uuid.UUID(slot))))
            .scalars()
            .all()
        )
        collision_audits = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "auth.login_failed",
                        AuditEvent.event_metadata["reason"].as_string() == "slot_collision",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(token_rows) == 1
    assert collision_audits == []


async def test_non_slot_integrity_failure_is_not_misreported_or_audited_as_collision(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the refresh-token PK constraint owns the durable collision taxonomy."""
    monkeypatch.setattr("app.services.auth_service.hash_refresh_token", lambda _token: "f" * 64)
    async with sessionmaker() as first:
        await AuthService(first, get_settings()).login(
            email=_DEV_EMAIL,
            password=_DEV_PASSWORD,
            session_id=uuid.UUID("69696969-6969-4969-8969-696969696969"),
        )
        await first.commit()
    async with sessionmaker() as second:
        with pytest.raises(IntegrityError):
            await AuthService(second, get_settings()).login(
                email=_DEV_EMAIL,
                password=_DEV_PASSWORD,
                session_id=uuid.UUID("70707070-7070-4070-8070-707070707070"),
            )
        await second.rollback()

    async with sessionmaker() as session:
        collision_audits = (
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.action == "auth.login_failed",
                        AuditEvent.event_metadata["reason"].as_string() == "slot_collision",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert collision_audits == []


async def test_slot_login_cap_bounds_active_rows_and_exact_cookie_namespace(
    app: FastAPI,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Admission is bounded while preserving previous + new selected slots (R3-003)."""
    bounded = get_settings().model_copy(update={"auth_session_max_active": 3})
    app.dependency_overrides[get_settings_dep] = lambda: bounded
    selected = "10101010-1010-4010-8010-101010101010"
    previous_header = "X-Lumen-Previous-Auth-Slot"
    max_set_cookie_headers = 0
    try:
        for index in range(50):
            slot = f"{index + 0x20000000:08x}-2020-4020-8020-{index + 1:012x}"
            response = await client.post(
                "/api/v1/auth/login",
                headers={
                    "X-Lumen-Auth-Slot": slot if index else selected,
                    **({previous_header: selected} if index else {}),
                },
                json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
            )
            assert response.status_code == 200
            max_set_cookie_headers = max(
                max_set_cookie_headers,
                len(response.headers.get_list("set-cookie")),
            )

        slot_cookies = [
            cookie
            for cookie in client.cookies.jar
            if cookie.name.startswith("lumen_refresh_token_")
        ]
        assert len(slot_cookies) <= 3
        assert any(cookie.name.endswith(selected) for cookie in slot_cookies)

        async with sessionmaker() as session:
            rows = (await session.execute(select(RefreshToken))).scalars().all()
        active = [row for row in rows if row.revoked_at is None]
        assert len(active) <= 3
        assert any(row.id == uuid.UUID(selected) for row in active)
        # One new cookie plus at most the bounded pre-admission family set. A
        # historical tombstone must not be re-emitted forever until response
        # headers themselves become the next unbounded namespace.
        assert max_set_cookie_headers <= 4
    finally:
        app.dependency_overrides.pop(get_settings_dep, None)


async def test_legacy_login_path_cannot_bypass_active_session_cap(
    app: FastAPI,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    bounded = get_settings().model_copy(update={"auth_session_max_active": 2})
    app.dependency_overrides[get_settings_dep] = lambda: bounded
    try:
        for _ in range(8):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
            )
            assert response.status_code == 200
        async with sessionmaker() as session:
            rows = (await session.execute(select(RefreshToken))).scalars().all()
        assert len([row for row in rows if row.revoked_at is None]) == 2
    finally:
        app.dependency_overrides.pop(get_settings_dep, None)


async def test_expired_previous_slot_is_deleted_while_new_slot_is_preserved(
    app: FastAPI,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    previous = uuid.UUID("18181818-1818-4818-8818-181818181818")
    new = uuid.UUID("28282828-2828-4828-8828-282828282828")
    async with sessionmaker() as session:
        user = (await session.execute(select(User).where(User.email == _DEV_EMAIL))).scalar_one()
        session.add(
            RefreshToken(
                id=previous,
                tenant_id=user.tenant_id,
                user_id=user.id,
                token_hash="a" * 64,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.commit()
    client.cookies.set(
        f"lumen_refresh_token_{previous}",
        "expired",
        domain="test.local",
        path="/api/v1/auth",
    )

    response = await client.post(
        "/api/v1/auth/login",
        headers={
            "X-Lumen-Auth-Slot": str(new),
            "X-Lumen-Previous-Auth-Slot": str(previous),
        },
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    assert response.status_code == 200
    set_cookies = response.headers.get_list("set-cookie")
    assert any(
        f"lumen_refresh_token_{previous}=" in value and "Max-Age=0" in value
        for value in set_cookies
    )
    assert any(
        f"lumen_refresh_token_{new}=" in value and "Max-Age=0" not in value for value in set_cookies
    )


async def test_oversized_owned_cookie_namespace_drains_in_bounded_header_batches(
    app: FastAPI,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A pre-existing excess cannot turn one login into an oversized response."""
    bounded = get_settings().model_copy(update={"auth_session_max_active": 3})
    app.dependency_overrides[get_settings_dep] = lambda: bounded
    selected = uuid.UUID("19191919-1919-4919-8919-191919191919")
    expires_at = datetime.now(UTC) + timedelta(days=1)
    seeded_slots = [
        selected,
        *(
            uuid.UUID(f"{index + 0x30000000:08x}-3030-4030-8030-{index + 1:012x}")
            for index in range(80)
        ),
    ]
    try:
        async with sessionmaker() as session:
            user = (
                await session.execute(select(User).where(User.email == _DEV_EMAIL))
            ).scalar_one()
            session.add_all(
                RefreshToken(
                    id=slot,
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    token_hash=f"{index + 1:064x}",
                    expires_at=expires_at,
                )
                for index, slot in enumerate(seeded_slots)
            )
            await session.commit()

        for index, slot in enumerate(seeded_slots):
            client.cookies.set(
                f"lumen_refresh_token_{slot}",
                f"stale-{index}",
                domain="test.local",
                path="/api/v1/auth",
            )
        # Attacker-controlled cookie names are only hints. Even strict-looking
        # canonical UUIDs that have no owned row must not amplify tombstones.
        forged_names: set[str] = set()
        for index in range(4):
            forged = uuid.UUID(f"{index + 0x50000000:08x}-5050-4050-8050-{index + 1:012x}")
            forged_name = f"lumen_refresh_token_{forged}"
            forged_names.add(forged_name)
            client.cookies.set(
                forged_name,
                "forged",
                domain="test.local",
                path="/api/v1/auth",
            )

        max_response_cookie_headers = 0
        max_response_cookie_bytes = 0
        max_request_cookie_bytes = 0
        for index in range(12):
            new_slot = uuid.UUID(f"{index + 0x40000000:08x}-4040-4040-8040-{index + 1:012x}")
            response = await client.post(
                "/api/v1/auth/login",
                headers={
                    "X-Lumen-Auth-Slot": str(new_slot),
                    "X-Lumen-Previous-Auth-Slot": str(selected),
                },
                json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
            )
            assert response.status_code == 200
            set_cookies = response.headers.get_list("set-cookie")
            response_cookie_bytes = sum(
                len(value.encode("latin-1")) + len(b"set-cookie: \r\n") for value in set_cookies
            )
            max_response_cookie_headers = max(max_response_cookie_headers, len(set_cookies))
            max_response_cookie_bytes = max(max_response_cookie_bytes, response_cookie_bytes)
            max_request_cookie_bytes = max(
                max_request_cookie_bytes,
                len(response.request.headers.get("cookie", "").encode("latin-1")),
            )
            remaining_owned = [
                cookie
                for cookie in client.cookies.jar
                if cookie.name.startswith("lumen_refresh_token_")
                and cookie.name not in forged_names
            ]
            if len(remaining_owned) <= 3:
                break

        assert max_response_cookie_headers <= 9  # one new value + eight tombstones
        assert max_response_cookie_bytes < 4096
        assert max_request_cookie_bytes < 8192
        assert len(remaining_owned) <= 3
        assert any(cookie.name.endswith(str(selected)) for cookie in remaining_owned)
        assert forged_names <= {cookie.name for cookie in client.cookies.jar}

        async with sessionmaker() as session:
            rows = (await session.execute(select(RefreshToken))).scalars().all()
        active = [row for row in rows if row.revoked_at is None]
        assert len(active) <= 3
        assert selected in {row.id for row in active}
    finally:
        app.dependency_overrides.pop(get_settings_dep, None)


def test_auth_session_cap_config_is_small_and_validated() -> None:
    assert 2 <= Settings().auth_session_max_active <= 16
    with pytest.raises(ValueError):
        Settings(AUTH_SESSION_MAX_ACTIVE=1)
    with pytest.raises(ValueError):
        Settings(AUTH_SESSION_MAX_ACTIVE=17)


async def test_malformed_auth_slot_is_rejected_without_creating_a_cookie(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": "not-a-uuid"},
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    assert response.status_code == 422
    assert "set-cookie" not in response.headers


async def test_non_v4_auth_slot_is_rejected_without_creating_a_cookie(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": "77777777-7777-1777-8777-777777777777"},
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    assert response.status_code == 422
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize(
    "value",
    [
        "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa",
        "{aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa}",
        "urn:uuid:aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    ],
)
async def test_noncanonical_auth_slot_spellings_are_rejected_without_side_effects(
    value: str,
    client: AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": value},
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    assert response.status_code == 422
    assert "set-cookie" not in response.headers
    async with sessionmaker() as session:
        assert (await session.execute(select(RefreshToken))).scalars().all() == []


async def test_noncanonical_auth_slot_is_rejected_on_refresh_and_logout(
    client: AsyncClient,
) -> None:
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    bearer = login.json()["access_token"]
    invalid = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"

    refresh = await client.post(
        "/api/v1/auth/refresh",
        headers={"X-Lumen-Auth-Slot": invalid},
    )
    logout = await client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {bearer}",
            "X-Lumen-Auth-Slot": invalid,
        },
    )
    assert refresh.status_code == logout.status_code == 422
    assert "set-cookie" not in refresh.headers
    assert "set-cookie" not in logout.headers


async def test_slot_cookie_is_secure_outside_local_environment(
    app: FastAPI,
    client: AsyncClient,
) -> None:
    production = get_settings().model_copy(update={"environment": "production"})
    app.dependency_overrides[get_settings_dep] = lambda: production
    slot = "88888888-8888-4888-8888-888888888888"
    headers = {"X-Lumen-Auth-Slot": slot}
    try:
        response = await client.post(
            "/api/v1/auth/login",
            headers=headers,
            json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
        )
        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {response.json()['access_token']}", **headers},
        )
    finally:
        app.dependency_overrides.pop(get_settings_dep, None)
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()
    deletion = logout.headers["set-cookie"].lower()
    assert "secure" in deletion
    assert "httponly" in deletion
    assert "samesite=strict" in deletion
    assert "path=/api/v1/auth" in deletion


async def test_cross_origin_auth_slot_preflight_fails_closed(client: AsyncClient) -> None:
    """The supported SPA transport is same-origin; backend CORS is not implicit."""
    response = await client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-lumen-auth-slot,content-type",
        },
    )
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-headers" not in response.headers


async def test_auth_flow_does_not_log_password_or_refresh_secret(
    client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = "log-sentinel-password-never-emit"
    response = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": "99999999-9999-4999-8999-999999999999"},
        json={"email": _DEV_EMAIL, "password": password},
    )
    assert response.status_code == 401
    assert password not in caplog.text

    caplog.clear()
    success = await client.post(
        "/api/v1/auth/login",
        headers={"X-Lumen-Auth-Slot": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD},
    )
    raw_refresh = success.cookies.get("lumen_refresh_token_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert raw_refresh is not None
    assert raw_refresh not in caplog.text


def test_auth_slot_contract_matches_emitted_server_protocol() -> None:
    contract = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"
    spec = yaml.safe_load(contract.read_text(encoding="utf-8"))
    emitted = create_app().openapi()
    for route in ("login", "refresh", "logout"):
        expected_ref = spec["paths"][f"/auth/{route}"]["post"]["parameters"][0]["$ref"]
        expected_name = expected_ref.rsplit("/", 1)[-1]
        expected = spec["components"]["parameters"][expected_name]
        actual = next(
            parameter
            for parameter in emitted["paths"][f"/api/v1/auth/{route}"]["post"]["parameters"]
            if parameter["name"] == "X-Lumen-Auth-Slot"
        )
        assert actual["required"] is expected["required"]
        assert actual["schema"] == expected["schema"]

    expected_previous = spec["components"]["parameters"]["PreviousAuthSlot"]
    actual_previous = next(
        parameter
        for parameter in emitted["paths"]["/api/v1/auth/login"]["post"]["parameters"]
        if parameter["name"] == "X-Lumen-Previous-Auth-Slot"
    )
    assert actual_previous["required"] is expected_previous["required"]
    assert actual_previous["schema"] == expected_previous["schema"]

    slot = spec["components"]["parameters"]["AuthSlot"]
    assert slot["schema"]["pattern"] == (
        "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert "409" in spec["paths"]["/auth/login"]["post"]["responses"]
    assert "422" in spec["paths"]["/auth/refresh"]["post"]["responses"]
    assert "422" in spec["paths"]["/auth/logout"]["post"]["responses"]


# --- Negative: authentication (INV-4) --------------------------------------


async def test_me_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_me_with_malformed_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


async def test_me_with_wrong_scheme_is_401(client: AsyncClient) -> None:
    # A non-bearer Authorization header is not accepted.
    resp = await client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc"})
    assert resp.status_code == 401


# --- Negative: credentials (generic 401, no disclosure) --------------------


async def test_login_wrong_password_is_generic_401(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": "wrong"})
    assert resp.status_code == 401
    detail = resp.json().get("detail", "")
    assert detail == "Invalid email or password."


async def test_login_unknown_account_is_same_generic_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "ghost@acme.test", "password": "whatever"}
    )
    assert resp.status_code == 401
    # Byte-identical to the wrong-password case → no account-existence disclosure.
    assert resp.json().get("detail", "") == "Invalid email or password."


# --- Negative: malformed body (INV-8) --------------------------------------


async def test_login_missing_password_is_422(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={"email": _DEV_EMAIL})
    assert resp.status_code == 422


async def test_login_empty_password_is_422(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": ""})
    assert resp.status_code == 422


async def test_login_unknown_field_is_422(client: AsyncClient) -> None:
    # extra=forbid on LoginRequest → an unexpected field is rejected.
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD, "tenant_id": "x"},
    )
    assert resp.status_code == 422


# --- Negative: role gate (INV-5) -------------------------------------------


async def test_require_roles_blocks_wrong_role_with_403(app: FastAPI, client: AsyncClient) -> None:
    """The reusable RBAC seam: a member hitting an admin-only route → 403."""

    # Mount a throwaway admin-only route on the same app to exercise the seam
    # the wave-2 endpoints will reuse (the dev user holds only `member`).
    @app.get("/api/v1/_admin_only")
    async def _admin_only(_: object = Depends(require_roles(Role.ADMIN))) -> dict[str, bool]:
        return {"ok": True}

    login = await client.post(
        "/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD}
    )
    token = login.json()["access_token"]

    # Authenticated but wrong role → 403 (distinct from the 401 of no token).
    forbidden = await client.get(
        "/api/v1/_admin_only", headers={"Authorization": f"Bearer {token}"}
    )
    assert forbidden.status_code == 403

    # No token at all → 401, not 403 (auth precedes authz).
    unauth = await client.get("/api/v1/_admin_only")
    assert unauth.status_code == 401


def test_require_roles_admits_correct_role() -> None:
    """A principal holding the required role passes the gate (positive control)."""
    gate = require_roles(Role.ADMIN, Role.SECURITY)
    admin = Principal(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), roles=(Role.ADMIN,))
    assert gate(admin) is admin


# --- Perf: a login must not park the worker (#512) -------------------------
#
# Argon2id verification is CPU-hard by design. Run inline on the event loop it
# serves nothing else for its whole duration, so an unauthenticated login flood
# stalls every other request in the process.
#
# The probe is a **handshake, not a stopwatch**: the stand-in verifier asks the
# event loop to release it and refuses to proceed until the loop actually does.
# On a worker thread the loop is free, runs the callback, and the verify carries
# on immediately. Run inline on the loop, the callback is queued behind the very
# verify that is waiting for it and can never fire — a deadlock the wait turns
# into a clear failure. Nothing here depends on how fast the machine is, so a
# loaded CI worker cannot flip the result (review round 1, finding 5).
#
# The stand-in replaces ``hashing._hasher`` — the one Argon2 object *both*
# ``verify_password`` and ``dummy_verify`` funnel through — so the probe measures
# loop residency rather than how the module happens to reach a thread.

# Only bounds the failure case: on success the handshake completes in microseconds.
_HANDSHAKE_TIMEOUT_SECONDS = 5.0


class _LoopHandshakeHasher:
    """Real hasher, but it will not verify until the event loop proves it is alive."""

    def __init__(self, inner: object, loop: asyncio.AbstractEventLoop) -> None:
        self._inner = inner
        self._loop = loop
        self.released_by_loop = False

    def verify(self, password_hash: str, password: str) -> bool:
        released = threading.Event()

        def _release() -> None:
            self.released_by_loop = True
            released.set()

        # Queued on the loop. It can only run if the loop is not sitting inside
        # this very call — which is exactly the property under test.
        self._loop.call_soon_threadsafe(_release)
        if not released.wait(timeout=_HANDSHAKE_TIMEOUT_SECONDS):
            raise AssertionError(
                "the event loop never ran while Argon2 verification was in progress "
                "— verification is blocking the loop"
            )
        # Delegate so mismatches still raise exactly what the wrappers catch.
        verified: bool = self._inner.verify(password_hash, password)  # type: ignore[attr-defined]
        return verified


def _install_handshake_hasher(monkeypatch: pytest.MonkeyPatch) -> _LoopHandshakeHasher:
    probe = _LoopHandshakeHasher(hashing._hasher, asyncio.get_running_loop())
    monkeypatch.setattr(hashing, "_hasher", probe)
    return probe


async def test_login_does_not_stall_the_event_loop(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known-user verify runs off the loop, so the loop keeps servicing work."""
    probe = _install_handshake_hasher(monkeypatch)

    resp = await client.post(
        "/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD}
    )

    # Unchanged outcome: the thread hop moves cost, never the verdict.
    assert resp.status_code == 200
    assert probe.released_by_loop is True


async def test_unknown_account_login_does_not_stall_the_event_loop(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-such-user path is the reachable one — it needs no valid account.

    ``dummy_verify`` burns a real verify's CPU so login timing cannot reveal
    whether an email exists (spec 0004 §2.3). That makes the *unauthenticated*
    path exactly as expensive as the authenticated one, so it must be off the
    loop too — otherwise anyone can stall the worker with invented addresses.
    """
    probe = _install_handshake_hasher(monkeypatch)

    resp = await client.post(
        "/api/v1/auth/login", json={"email": "ghost@acme.test", "password": "x"}
    )

    # Still the same generic 401 — no observable outcome changes.
    assert resp.status_code == 401
    assert resp.json().get("detail", "") == "Invalid email or password."
    assert probe.released_by_loop is True
