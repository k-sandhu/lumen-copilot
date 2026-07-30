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
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Iterator

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session, require_roles
from app.auth import Principal, hash_password, hashing
from app.db.base import Base
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app

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
    # After logout the refresh token is revoked → refresh now 401s.
    again = await client.post("/api/v1/auth/refresh")
    assert again.status_code == 401


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
# The probe is a 10 ms ticker coroutine racing the login, and the assertion is on
# the **longest gap** between its wakeups — not on a tick count. A login awaits
# the database several times either way, so a count accumulates enough ticks
# around the stall to pass even when the loop *was* parked; the longest gap
# isolates exactly the unresponsive stretch this issue is about.
#
# The stall is injected at ``hashing._hasher`` — the one Argon2 object *both*
# ``verify_password`` and ``dummy_verify`` funnel through. Patching there (rather
# than either wrapper) keeps the probe independent of how the module routes work
# to a thread, so the test measures loop residency and not an implementation
# detail. A fixed stall also keeps it fast and deterministic; real Argon2 timing
# would be neither.

_STALL_SECONDS = 0.3
_TICK_SECONDS = 0.01
# Verification on the loop produces a gap of >= _STALL_SECONDS; off the loop the
# gaps stay at tick resolution. 0.1 s sits an order of magnitude above Windows
# timer jitter and 3x below the stall, so the two regimes cannot be confused.
_MAX_GAP_SECONDS = 0.1


class _StallingHasher:
    """The real hasher plus a fixed stall — same verdicts, deliberate cost."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def verify(self, password_hash: str, password: str) -> bool:
        time.sleep(_STALL_SECONDS)
        # Delegate so mismatches still raise exactly what the wrappers catch.
        verified: bool = self._inner.verify(password_hash, password)  # type: ignore[attr-defined]
        return verified


async def _longest_loop_gap_during(awaitable: Awaitable[Response]) -> tuple[Response, float]:
    """Await ``awaitable``; return it with the longest event-loop stall observed."""
    gaps: list[float] = []

    async def _ticker() -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    try:
        await asyncio.sleep(0)  # let the ticker reach its first await
        result = await awaitable
    finally:
        ticker.cancel()
    # No tick at all means the loop never came back — the worst case, not a pass.
    return result, max(gaps, default=float("inf"))


async def test_login_does_not_stall_the_event_loop(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The known-user verify runs off the loop, so concurrent work keeps running."""
    monkeypatch.setattr(hashing, "_hasher", _StallingHasher(hashing._hasher))

    resp, longest_gap = await _longest_loop_gap_during(
        client.post("/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD})
    )
    # Unchanged outcome: the thread hop moves cost, never the verdict.
    assert resp.status_code == 200
    assert longest_gap < _MAX_GAP_SECONDS


async def test_unknown_account_login_does_not_stall_the_event_loop(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-such-user path is the reachable one — it needs no valid account.

    ``dummy_verify`` burns a real verify's CPU so login timing cannot reveal
    whether an email exists (spec 0004 §2.3). That makes the *unauthenticated*
    path exactly as expensive as the authenticated one, so it must be off the
    loop too — otherwise anyone can stall the worker with invented addresses.
    """
    monkeypatch.setattr(hashing, "_hasher", _StallingHasher(hashing._hasher))

    resp, longest_gap = await _longest_loop_gap_during(
        client.post("/api/v1/auth/login", json={"email": "ghost@acme.test", "password": "x"})
    )
    # Still the same generic 401 — no observable outcome changes.
    assert resp.status_code == 401
    assert resp.json().get("detail", "") == "Invalid email or password."
    assert longest_gap < _MAX_GAP_SECONDS
