"""Model-picker API tests — the GET /models contract + negatives (#47).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite
database (no Postgres needed), the same way ``test_auth_api`` does: the app's
``get_db_session`` dependency is overridden to yield sessions from a StaticPool
SQLite engine whose schema is built from the ORM metadata, and a dev user is
seeded so a real bearer token can be minted via /auth/login.

Covers:
* AC-1: GET /models returns the curated registry per the contract
  (ChatModelInfo: id/label/provider/tier/is_default), grouped frontier/fast/oss,
  with EXACTLY ONE is_default;
* AC-2: the seed set matches the config-driven registry;
* AC-4 (INV-4): unauthenticated GET /models → 401.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session
from app.auth import hash_password
from app.db.base import Base
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.domain.models import ModelTier
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_DEV_EMAIL = "dev@acme.test"
_DEV_PASSWORD = "devpassword"

# The seed registry (core/config._DEFAULT_CHAT_MODEL_REGISTRY) the picker serves
# when CHAT_MODEL_REGISTRY is unset — the offline conftest leaves it unset.
_EXPECTED_FRONTIER = {"anthropic/claude-opus-4.8", "openai/gpt-5.5"}
_EXPECTED_FAST = {"google/gemini-3.5-flash", "anthropic/claude-haiku-4.5"}
_EXPECTED_OSS = {"deepseek/deepseek-v3.2", "qwen/qwen3.7-max"}
_DEFAULT_MODEL_ID = "anthropic/claude-opus-4.8"


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


async def _login(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": _DEV_EMAIL, "password": _DEV_PASSWORD}
    )
    assert resp.status_code == 200
    return str(resp.json()["access_token"])


# --- Happy path (AC-1 / AC-2) ----------------------------------------------


async def test_list_models_returns_registry_shape(client: AsyncClient) -> None:
    token = await _login(client)
    resp = await client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items"}
    items = body["items"]
    assert items, "registry must not be empty"

    # Every entry matches ChatModelInfo's required fields with valid types.
    valid_tiers = {t.value for t in ModelTier}
    for item in items:
        assert set(item) <= {"id", "label", "provider", "tier", "is_default", "description"}
        assert {"id", "label", "provider", "tier", "is_default"} <= set(item)
        assert isinstance(item["id"], str) and item["id"]
        assert isinstance(item["label"], str) and item["label"]
        assert isinstance(item["provider"], str) and item["provider"]
        assert item["tier"] in valid_tiers
        assert isinstance(item["is_default"], bool)


async def test_list_models_has_exactly_one_default(client: AsyncClient) -> None:
    token = await _login(client)
    resp = await client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
    items = resp.json()["items"]

    defaults = [m for m in items if m["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == _DEFAULT_MODEL_ID


async def test_list_models_groups_frontier_fast_oss(client: AsyncClient) -> None:
    token = await _login(client)
    resp = await client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
    items = resp.json()["items"]

    by_tier: dict[str, set[str]] = {"frontier": set(), "fast": set(), "oss": set()}
    for m in items:
        by_tier[m["tier"]].add(m["id"])

    assert by_tier["frontier"] == _EXPECTED_FRONTIER
    assert by_tier["fast"] == _EXPECTED_FAST
    assert by_tier["oss"] == _EXPECTED_OSS


# --- Negative: authentication (INV-4 / AC-4) -------------------------------


async def test_list_models_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/models")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_list_models_with_malformed_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/models", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401
