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

import uuid
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
from app.db.repositories import LlmProviderRepository, TenantRepository, UserRepository
from app.domain.entities import LlmProviderStatus, Role
from app.domain.models import ModelTier
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_DEV_EMAIL = "dev@acme.test"
_DEV_PASSWORD = "devpassword"
# A second tenant + its user, so cross-tenant provider isolation is exercised.
_OTHER_EMAIL = "dev@globex.test"

# The seed registry (core/config._DEFAULT_CHAT_MODEL_REGISTRY) the picker serves
# when CHAT_MODEL_REGISTRY is unset — the offline conftest leaves it unset.
_EXPECTED_FRONTIER = {"openrouter/anthropic/claude-opus-4.8", "openrouter/openai/gpt-5.5"}
_EXPECTED_FAST = {"openrouter/google/gemini-3.5-flash", "openrouter/anthropic/claude-haiku-4.5"}
_EXPECTED_OSS = {"openrouter/deepseek/deepseek-v3.2", "openrouter/qwen/qwen3.7-max"}
_DEFAULT_MODEL_ID = "openrouter/anthropic/claude-opus-4.8"


async def _seed_provider(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    name: str,
    base_url: str,
    models: list[dict[str, object]],
    enabled: bool,
) -> uuid.UUID:
    """Seed one llm_providers row (READY, with a discovered-model snapshot).

    Uses the real tenant-scoped repository: ``create`` (enabled + empty snapshot),
    ``set_discovery`` to attach the ``discovered_models``, then ``update`` to toggle
    ``enabled`` when a disabled provider is wanted. Returns the provider id.
    """
    repo = LlmProviderRepository(session, tenant_id)
    provider = await repo.create(
        owner_id=owner_id,
        name=name,
        provider_type="openai_compatible",
        base_url=base_url,
        api_key_secret_ref=None,
        secret_hint=None,
    )
    await repo.set_discovery(
        provider.id,
        status=LlmProviderStatus.READY,
        discovered_models=models,
        last_error=None,
        last_discovery_at=None,
    )
    if not enabled:
        await repo.update(provider.id, enabled=False)
    return provider.id


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A StaticPool SQLite engine + schema; seed two tenants + provider rows.

    Tenant A (the dev user) has one ENABLED provider (two discovered models) and
    one DISABLED provider; tenant B (the other user) has its own enabled provider —
    so the surfacing + isolation assertions have real data to read.
    """
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
            dev = await UserRepository(seed_session, tenant.id).create(
                email=_DEV_EMAIL,
                password_hash=hash_password(_DEV_PASSWORD),
                roles=[Role.MEMBER],
            )
            other_tenant = await TenantRepository(seed_session).create(name="Globex")
            other = await UserRepository(seed_session, other_tenant.id).create(
                email=_OTHER_EMAIL,
                password_hash=hash_password(_DEV_PASSWORD),
                roles=[Role.MEMBER],
            )
            enabled_id = await _seed_provider(
                seed_session,
                tenant_id=tenant.id,
                owner_id=dev.id,
                name="Acme OpenAI",
                base_url="https://provider-a.example.com/v1",
                models=[
                    {"id": "openai/gpt-4o", "label": "GPT-4o"},
                    {"id": "meta-llama/llama-3-70b", "label": None},
                ],
                enabled=True,
            )
            await _seed_provider(
                seed_session,
                tenant_id=tenant.id,
                owner_id=dev.id,
                name="Acme Disabled",
                base_url="https://provider-b.example.com/v1",
                models=[{"id": "anthropic/claude-3", "label": "Claude 3"}],
                enabled=False,
            )
            await _seed_provider(
                seed_session,
                tenant_id=other_tenant.id,
                owner_id=other.id,
                name="Globex OpenAI",
                base_url="https://provider-c.example.com/v1",
                models=[{"id": "globex/secret-model", "label": "Secret"}],
                enabled=True,
            )
            await seed_session.commit()
            factory.enabled_provider_id = enabled_id  # type: ignore[attr-defined]
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


@pytest.fixture
def enabled_provider_id(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """The id of tenant A's ENABLED seeded provider (for namespaced-id assertions)."""
    return sessionmaker.enabled_provider_id  # type: ignore[attr-defined, no-any-return]


async def _login(client: AsyncClient, email: str = _DEV_EMAIL) -> str:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": _DEV_PASSWORD}
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

    # The config registry's tier grouping is unchanged by PR 2a. Provider models are
    # additive (they surface under ``frontier`` with a namespaced ``provider:`` id),
    # so restrict this config-registry assertion to the non-provider (config) ids.
    by_tier: dict[str, set[str]] = {"frontier": set(), "fast": set(), "oss": set()}
    for m in items:
        if m["id"].startswith("provider:"):
            continue
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


# --- Per-tenant provider models surfaced in the picker (PR 2a) -------------


async def test_enabled_provider_models_appear_with_namespaced_ids(
    client: AsyncClient, enabled_provider_id: uuid.UUID
) -> None:
    # The tenant's ENABLED provider's discovered models surface with the
    # ``provider:{provider_id}:{raw_model_id}`` id, a ``{model} · {provider}`` label,
    # and provider = the provider name — alongside the unchanged config models.
    token = await _login(client)
    resp = await client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    by_id = {m["id"]: m for m in items}

    gpt = f"provider:{enabled_provider_id}:openai/gpt-4o"
    llama = f"provider:{enabled_provider_id}:meta-llama/llama-3-70b"
    assert gpt in by_id, "enabled provider's models must be selectable"
    assert llama in by_id
    assert by_id[gpt]["label"] == "GPT-4o · Acme OpenAI"
    assert by_id[gpt]["provider"] == "Acme OpenAI"
    assert by_id[gpt]["is_default"] is False
    # A discovered entry with a null label falls back to its raw id in the label.
    assert by_id[llama]["label"] == "meta-llama/llama-3-70b · Acme OpenAI"
    # The config registry is still present and unchanged (namespacing is additive).
    assert _DEFAULT_MODEL_ID in by_id


async def test_disabled_provider_models_absent(
    client: AsyncClient, enabled_provider_id: uuid.UUID
) -> None:
    # The DISABLED provider (Acme Disabled → anthropic/claude-3) contributes nothing.
    token = await _login(client)
    resp = await client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
    ids = {m["id"] for m in resp.json()["items"]}
    assert not any(":anthropic/claude-3" in i for i in ids)
    # And a provider-prefixed id only ever names THIS tenant's enabled provider.
    provider_ids = {i for i in ids if i.startswith("provider:")}
    assert provider_ids and all(str(enabled_provider_id) in i for i in provider_ids)


async def test_other_tenant_provider_models_absent(client: AsyncClient) -> None:
    # Tenant A's picker never shows tenant B's provider models (INV-1).
    token = await _login(client)
    resp = await client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
    ids = {m["id"] for m in resp.json()["items"]}
    assert not any("globex/secret-model" in i for i in ids)


async def test_other_tenant_sees_only_its_own_provider_models(client: AsyncClient) -> None:
    # Symmetric check from tenant B's side: it sees its own provider, not tenant A's.
    token = await _login(client, _OTHER_EMAIL)
    resp = await client.get("/api/v1/models", headers={"Authorization": f"Bearer {token}"})
    ids = {m["id"] for m in resp.json()["items"]}
    assert any("globex/secret-model" in i for i in ids)
    assert not any("openai/gpt-4o" in i for i in ids if i.startswith("provider:"))
