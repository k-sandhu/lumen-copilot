"""Preferences API tests — GET/PATCH /preferences (spec 0005, epic #144).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB
(mirrors test_chat_api). Covers:

* a fresh user reads the implicit default state (``default_model_id``/``updated_at``
  null) and the read creates **no** row (read-before-write, AC-P1);
* PATCH sets / clears the default model and GET reflects it (AC-P2);
* an unknown ``default_model_id`` → 422, nothing persisted (AC-P3);
* a new chat session with no model uses the caller's preference default (AC-P4);
* per-user isolation: one user's preference never appears for another, same or
  different tenant (INV-1/INV-2);
* no/empty token → 401 (INV-4).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session
from app.auth import hash_password
from app.db.base import Base
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app

import app.db.models as models  # noqa: E402  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(self, *, alice_id: uuid.UUID, bob_id: uuid.UUID, carol_id: uuid.UUID) -> None:
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.carol_id = carol_id
        self.alice_email = "alice@acme.test"
        self.bob_email = "bob@acme.test"
        self.carol_email = "carol@globex.test"


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            ta = await TenantRepository(seed).create(name="Acme")
            tb = await TenantRepository(seed).create(name="Globex")
            alice = await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            carol = await UserRepository(seed, tb.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                alice_id=alice.id, bob_id=bob.id, carol_id=carol.id
            )
            yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> Iterator[FastAPI]:
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


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _a_non_default_model(client: AsyncClient, token: str) -> str:
    """A real registry id that is NOT the server default (proves the override took)."""
    resp = await client.get("/api/v1/models", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    non_default = [m["id"] for m in items if not m["is_default"]]
    assert non_default, "registry should offer >1 model for this test"
    return str(non_default[0])


async def _count_preference_rows(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    async with sessionmaker() as s:
        return int(
            (await s.execute(select(func.count()).select_from(models.UserPreference))).scalar_one()
        )


# --- AC-P1: fresh user, read-before-write ----------------------------------


async def test_fresh_user_gets_implicit_default_without_writing(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/preferences", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"default_model_id": None, "custom_instructions": None, "updated_at": None}
    # The read must NOT have created a row (read-before-write).
    assert await _count_preference_rows(sessionmaker) == 0


# --- AC-P2: set / clear ----------------------------------------------------


async def test_set_then_clear_default_model(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    model = await _a_non_default_model(client, token)

    set_resp = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"default_model_id": model}
    )
    assert set_resp.status_code == 200, set_resp.text
    body = set_resp.json()
    assert body["default_model_id"] == model
    assert body["updated_at"] is not None

    got = await client.get("/api/v1/preferences", headers=_auth(token))
    assert got.json()["default_model_id"] == model

    cleared = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"default_model_id": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["default_model_id"] is None


# --- AC-P3: unknown model rejected -----------------------------------------


async def test_unknown_default_model_is_422(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"default_model_id": "bogus/model"}
    )
    assert resp.status_code == 422
    assert await _count_preference_rows(sessionmaker) == 0


async def test_patch_empty_body_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.patch("/api/v1/preferences", headers=_auth(token), json={})
    assert resp.status_code == 422


# --- AC-P4: a new chat session uses the preference --------------------------


async def test_new_session_uses_preference_default(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    model = await _a_non_default_model(client, token)
    await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"default_model_id": model}
    )
    # Create a session with NO explicit model → it should inherit the preference.
    created = await client.post("/api/v1/chat/sessions", headers=_auth(token), json={"title": "x"})
    assert created.status_code == 201, created.text
    assert created.json()["model"] == model


# --- Isolation (INV-1/INV-2) + auth (INV-4) --------------------------------


async def test_preferences_are_per_user(client: AsyncClient, seeded: _Seeded) -> None:
    alice = await _login(client, seeded.alice_email)
    model = await _a_non_default_model(client, alice)
    await client.patch(
        "/api/v1/preferences", headers=_auth(alice), json={"default_model_id": model}
    )

    # Bob (same tenant) and Carol (other tenant) are unaffected — fresh defaults.
    for email in (seeded.bob_email, seeded.carol_email):
        other = await _login(client, email)
        resp = await client.get("/api/v1/preferences", headers=_auth(other))
        assert resp.status_code == 200
        assert resp.json() == {
            "default_model_id": None,
            "custom_instructions": None,
            "updated_at": None,
        }


async def test_preferences_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/preferences")).status_code == 401
    assert (
        await client.patch("/api/v1/preferences", json={"default_model_id": None})
    ).status_code == 401


# --- Custom instructions: get / set / clear / cap ---------------------------


async def test_fresh_user_custom_instructions_null(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/preferences", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["custom_instructions"] is None


async def test_set_then_clear_custom_instructions(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    text = "Always answer concisely and cite sources."

    set_resp = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"custom_instructions": text}
    )
    assert set_resp.status_code == 200, set_resp.text
    body = set_resp.json()
    assert body["custom_instructions"] == text
    assert body["updated_at"] is not None

    got = await client.get("/api/v1/preferences", headers=_auth(token))
    assert got.json()["custom_instructions"] == text

    cleared = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"custom_instructions": None}
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["custom_instructions"] is None


async def test_blank_custom_instructions_clears(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"custom_instructions": "hi there"}
    )
    # A whitespace-only string normalizes to None (clear), not an empty stored value.
    resp = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"custom_instructions": "   "}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["custom_instructions"] is None


async def test_custom_instructions_over_limit_is_422(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    too_long = "x" * 2001
    resp = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"custom_instructions": too_long}
    )
    assert resp.status_code == 422, resp.text
    # Nothing persisted on rejection.
    assert await _count_preference_rows(sessionmaker) == 0


async def test_custom_instructions_and_default_model_are_independent(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    model = await _a_non_default_model(client, token)
    # Set only the default model — custom_instructions stays untouched (unset).
    await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"default_model_id": model}
    )
    # Now set only custom_instructions — the default model must NOT be cleared.
    resp = await client.patch(
        "/api/v1/preferences", headers=_auth(token), json={"custom_instructions": "Be brief."}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["custom_instructions"] == "Be brief."
    assert body["default_model_id"] == model


async def test_custom_instructions_are_per_user(client: AsyncClient, seeded: _Seeded) -> None:
    alice = await _login(client, seeded.alice_email)
    await client.patch(
        "/api/v1/preferences", headers=_auth(alice), json={"custom_instructions": "Alice only."}
    )
    # Bob (same tenant) and Carol (other tenant) are unaffected (INV-1/INV-2).
    for email in (seeded.bob_email, seeded.carol_email):
        other = await _login(client, email)
        resp = await client.get("/api/v1/preferences", headers=_auth(other))
        assert resp.status_code == 200
        assert resp.json()["custom_instructions"] is None
