"""Saved-searches API tests — CRUD over /saved-searches (spec 0005, epic #144).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB
(mirrors test_preferences_api). Covers:

* create + get round-trip incl. the filters (AC-S1/AC-S2);
* list newest-first; update (rename / change / clear a filter); delete → 404;
* negatives: another user's (same or other tenant) saved search → 404 on
  get/patch/delete (INV-1/INV-2); over-long name → 422; empty PATCH body → 422;
  no token → 401 (INV-4).
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

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(self) -> None:
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
            await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            await UserRepository(seed, tb.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded()  # type: ignore[attr-defined]
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


_BASE = "/api/v1/saved-searches"


# --- AC-S1 / AC-S2: create + get round-trip with filters --------------------


async def test_create_and_get_round_trips_filters(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    coll = str(uuid.uuid4())
    payload = {
        "name": "Renewals at risk",
        "query": "acme renewal risk",
        "collection_id": coll,
        "source": "upload",
        "type": "document",
    }
    created = await client.post(_BASE, headers=_auth(token), json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Renewals at risk"
    assert body["query"] == "acme renewal risk"
    assert body["collection_id"] == coll
    assert body["source"] == "upload"
    assert body["type"] == "document"

    got = await client.get(f"{_BASE}/{body['id']}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json() == body


async def test_list_returns_all_in_deterministic_order(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    a = await client.post(_BASE, headers=_auth(token), json={"name": "one", "query": "a"})
    b = await client.post(_BASE, headers=_auth(token), json={"name": "two", "query": "b"})
    listing = await client.get(_BASE, headers=_auth(token))
    assert listing.status_code == 200
    items = listing.json()["items"]
    # Both are returned, scoped to the caller.
    assert {i["id"] for i in items} == {a.json()["id"], b.json()["id"]}
    # …in the documented deterministic order — (updated_at, id) descending — so
    # paging is stable even when two rows share a creation instant.
    expected = sorted(items, key=lambda i: (i["updated_at"], i["id"]), reverse=True)
    assert items == expected


# --- update: rename / change / clear a filter -------------------------------


async def test_update_renames_changes_and_clears_filter(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        _BASE, headers=_auth(token), json={"name": "n", "query": "q", "source": "upload"}
    )
    sid = created.json()["id"]

    renamed = await client.patch(
        f"{_BASE}/{sid}", headers=_auth(token), json={"name": "renamed", "query": "q2"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "renamed"
    assert renamed.json()["query"] == "q2"
    # The source we didn't touch is preserved.
    assert renamed.json()["source"] == "upload"

    # Clear the source explicitly (tri-state null).
    cleared = await client.patch(f"{_BASE}/{sid}", headers=_auth(token), json={"source": None})
    assert cleared.status_code == 200
    assert cleared.json()["source"] is None


async def test_delete_then_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(_BASE, headers=_auth(token), json={"name": "n", "query": "q"})
    sid = created.json()["id"]
    deleted = await client.delete(f"{_BASE}/{sid}", headers=_auth(token))
    assert deleted.status_code == 204
    assert (await client.get(f"{_BASE}/{sid}", headers=_auth(token))).status_code == 404


# --- Isolation (INV-1/INV-2) ------------------------------------------------


@pytest.mark.parametrize("other_email", ["bob@acme.test", "carol@globex.test"])
async def test_other_user_cannot_access(
    client: AsyncClient, seeded: _Seeded, other_email: str, durable_audit_ledger
) -> None:
    alice = await _login(client, seeded.alice_email)
    created = await client.post(_BASE, headers=_auth(alice), json={"name": "n", "query": "q"})
    sid = created.json()["id"]

    other = await _login(client, other_email)
    assert (await client.get(f"{_BASE}/{sid}", headers=_auth(other))).status_code == 404
    assert (
        await client.patch(f"{_BASE}/{sid}", headers=_auth(other), json={"name": "x"})
    ).status_code == 404
    assert (await client.delete(f"{_BASE}/{sid}", headers=_auth(other))).status_code == 404
    assert [event.metadata["attempted_action"] for event in durable_audit_ledger.events] == [
        "saved_search.read",
        "saved_search.update",
        "saved_search.delete",
    ]
    # The other user's own list is empty — they never see Alice's saved search.
    listing = await client.get(_BASE, headers=_auth(other))
    assert listing.json()["items"] == []


# --- Malformed input (INV-8) + auth (INV-4) ---------------------------------


async def test_over_long_name_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(_BASE, headers=_auth(token), json={"name": "x" * 201, "query": "q"})
    assert resp.status_code == 422


async def test_empty_patch_body_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(_BASE, headers=_auth(token), json={"name": "n", "query": "q"})
    sid = created.json()["id"]
    resp = await client.patch(f"{_BASE}/{sid}", headers=_auth(token), json={})
    assert resp.status_code == 422


@pytest.mark.parametrize("field", ["name", "query"])
async def test_patch_explicit_null_name_or_query_is_422(
    client: AsyncClient, seeded: _Seeded, field: str
) -> None:
    # name/query are non-nullable in the contract: an EXPLICIT null is malformed
    # (422), not a silent no-op (only collection_id/source/type are tri-state).
    token = await _login(client, seeded.alice_email)
    created = await client.post(_BASE, headers=_auth(token), json={"name": "n", "query": "q"})
    sid = created.json()["id"]
    resp = await client.patch(f"{_BASE}/{sid}", headers=_auth(token), json={field: None})
    assert resp.status_code == 422


async def test_requires_auth(client: AsyncClient) -> None:
    assert (await client.get(_BASE)).status_code == 401
    assert (await client.post(_BASE, json={"name": "n", "query": "q"})).status_code == 401
