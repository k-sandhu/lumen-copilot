"""Sources API tests — the /sources contract + required negatives (#20, ADR-0009).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite DB
(no Postgres) with the sync enqueue faked (no broker). The sources router is
auto-discovered (no aggregator edit), so hitting ``/api/v1/sources`` exercises the
real router/service/connector/repository wiring.

Covered:

* happy path: add (201, status=pending) → list → sync (202 → syncing) →
  delete (204); the wire ``Source`` carries ``{url, mode}`` config only (no
  internal ``collection_id``);
* SSRF: an SSRF-blocked URL → **422** with Problem ``code = url_blocked``
  (INV-8 / ADR-0009 §3); an unknown ``type`` → 422 ``unsupported_source_type``;
  a malformed body → 422;
* INV-1/INV-2: another tenant's or another user's source on sync / delete →
  **404** (never 403, existence non-disclosure); list is owner-scoped;
* INV-4: no / malformed bearer → 401.
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
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PASSWORD = "devpassword"
_PUBLIC = "93.184.216.34"


class _Seeded:
    def __init__(self, *, tenant_a: uuid.UUID, tenant_b: uuid.UUID) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_email = "alice@acme.test"  # tenant A owner under test
        self.bob_email = "bob@acme.test"  # tenant A, other owner
        self.carol_email = "carol@globex.test"  # tenant B owner


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
            tenant_a = await TenantRepository(seed).create(name="Acme")
            tenant_b = await TenantRepository(seed).create(name="Globex")
            await UserRepository(seed, tenant_a.id).create(
                email="alice@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            await UserRepository(seed, tenant_a.id).create(
                email="bob@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            await UserRepository(seed, tenant_b.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await seed.commit()
        factory.seeded = _Seeded(tenant_a=tenant_a.id, tenant_b=tenant_b.id)  # type: ignore[attr-defined]
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture(autouse=True)
def no_broker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Capture sync enqueues without a broker (offline-safe)."""
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_source_sync",
        lambda tid, sid: calls.append((tid, sid)),
    )
    return calls


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


async def _add(client: AsyncClient, token: str, url: str) -> dict:
    resp = await client.post(
        "/api/v1/sources", json={"type": "web", "url": url}, headers=_auth(token)
    )
    return resp


# --- happy path -------------------------------------------------------------


async def test_add_list_sync_delete_round_trip(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)

    # add → 201 pending
    resp = await _add(client, token, f"http://{_PUBLIC}/page")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "web"
    assert body["status"] == "pending"
    # The frozen SourceConfig marks BOTH url and mode required: the 201 response
    # carries the exact {url, mode} shape — mode is non-null (seeded from a URL
    # heuristic at creation), and the internal collection_id is NOT exposed
    # (SourceConfig additionalProperties: false). Regression for the review
    # finding that `mode` was omitted (response_model_exclude_none dropped it).
    assert body["config"] == {"url": f"http://{_PUBLIC}/page", "mode": "page"}
    source_id = body["id"]

    # list → the one source
    resp = await client.get("/api/v1/sources", headers=_auth(token))
    assert resp.status_code == 200
    listed = resp.json()
    assert [s["id"] for s in listed["items"]] == [source_id]

    # sync → 202 syncing
    resp = await client.post(f"/api/v1/sources/{source_id}/sync", headers=_auth(token))
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "syncing"

    # sync again while syncing → 200 no-op
    resp = await client.post(f"/api/v1/sources/{source_id}/sync", headers=_auth(token))
    assert resp.status_code == 200

    # delete → 204
    resp = await client.delete(f"/api/v1/sources/{source_id}", headers=_auth(token))
    assert resp.status_code == 204

    # gone from the list
    resp = await client.get("/api/v1/sources", headers=_auth(token))
    assert resp.json()["items"] == []


async def test_add_feed_url_seeds_mode_feed(client: AsyncClient, seeded: _Seeded) -> None:
    """A feed-like URL seeds ``mode=feed`` at creation (contract-required, non-null)."""
    token = await _login(client, seeded.alice_email)
    resp = await _add(client, token, f"http://{_PUBLIC}/blog/rss")
    assert resp.status_code == 201, resp.text
    assert resp.json()["config"] == {"url": f"http://{_PUBLIC}/blog/rss", "mode": "feed"}


# --- SSRF / validation negatives (422) --------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/internal",
        "ftp://example.com/x",
    ],
)
async def test_add_ssrf_url_is_422_url_blocked(
    client: AsyncClient, seeded: _Seeded, url: str
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await _add(client, token, url)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "url_blocked"


async def test_add_empty_url_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/sources", json={"type": "web", "url": ""}, headers=_auth(token)
    )
    # An empty url is an invalid config (422).
    assert resp.status_code == 422


async def test_add_unknown_type_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/sources",
        json={"type": "dropbox", "url": f"http://{_PUBLIC}/x"},
        headers=_auth(token),
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "unsupported_source_type"


async def test_add_malformed_body_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/sources", json={"type": "web"}, headers=_auth(token)
    )  # missing url
    assert resp.status_code == 422


# --- INV-1/INV-2: cross-tenant + cross-owner → 404 --------------------------


async def test_sync_other_owner_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    bob_token = await _login(client, seeded.bob_email)
    resp = await _add(client, bob_token, f"http://{_PUBLIC}/bob")
    bob_source_id = resp.json()["id"]

    alice_token = await _login(client, seeded.alice_email)
    resp = await client.post(f"/api/v1/sources/{bob_source_id}/sync", headers=_auth(alice_token))
    assert resp.status_code == 404  # never 403 (existence non-disclosure)


async def test_delete_cross_tenant_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    alice_token = await _login(client, seeded.alice_email)
    resp = await _add(client, alice_token, f"http://{_PUBLIC}/a")
    source_id = resp.json()["id"]

    carol_token = await _login(client, seeded.carol_email)  # tenant B
    resp = await client.delete(f"/api/v1/sources/{source_id}", headers=_auth(carol_token))
    assert resp.status_code == 404


async def test_list_is_owner_scoped(client: AsyncClient, seeded: _Seeded) -> None:
    alice_token = await _login(client, seeded.alice_email)
    await _add(client, alice_token, f"http://{_PUBLIC}/alice")

    bob_token = await _login(client, seeded.bob_email)
    await _add(client, bob_token, f"http://{_PUBLIC}/bob")

    resp = await client.get("/api/v1/sources", headers=_auth(alice_token))
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["config"]["url"] == f"http://{_PUBLIC}/alice"


async def test_get_missing_source_sync_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(f"/api/v1/sources/{uuid.uuid4()}/sync", headers=_auth(token))
    assert resp.status_code == 404


# --- INV-4: auth ------------------------------------------------------------


async def test_unauthenticated_list_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/sources")
    assert resp.status_code == 401


async def test_malformed_bearer_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/sources", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


async def test_unauthenticated_add_is_401(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/sources", json={"type": "web", "url": "http://x/y"})
    assert resp.status_code == 401
