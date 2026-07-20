"""Collections API tests — the /collections contract + required negatives (#46).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite
database (no Postgres needed), mirroring ``test_auth_api``: the app's
``get_db_session`` dependency is overridden to yield sessions from a StaticPool
SQLite engine whose schema is created from the ORM metadata. Two users in two
tenants are seeded so the ownership/tenancy negatives are real:

* happy path: create → list (cursor page) → get → patch → delete (cascades);
* ``document_count`` reflects contained documents;
* audit events emitted on create + delete (``collection.created`` /
  ``collection.deleted``);
* negatives (spec 0004 §3):
  - INV-1/INV-2: another tenant's or another user's collection on
    GET/PATCH/DELETE → **404** (never 403, existence non-disclosure);
  - INV-4: no / malformed bearer → 401;
  - INV-8: malformed body (empty/missing/too-long name, unknown field, empty
    PATCH) → 422.
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

from app.api.deps import get_db_session, get_object_store_dep
from app.auth import hash_password
from app.core.errors import NotFoundError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.main import create_app
from app.storage.keys import assert_key_owned_by, build_key
from app.storage.object_store import StoredObject

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PASSWORD = "devpassword"


class FakeObjectStore:
    """In-memory stand-in for the #22 ``ObjectStore`` (no MinIO on delete paths).

    Mirrors the surface ``CollectionsService`` relies on: content-addressed keys
    and a tenant-prefix-checked, idempotent ``delete`` that records removed keys
    so the delete route never reaches real MinIO.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> StoredObject:
        key = build_key(tenant_id, data, filename)
        self.objects[key] = data
        return StoredObject(
            key=key, sha256=key.split("/")[1], size_bytes=len(data), content_type=content_type
        )

    async def get(self, tenant_id: str, key: str) -> bytes:
        assert_key_owned_by(key, tenant_id)
        if key not in self.objects:
            raise NotFoundError("object not found", code="object_not_found")
        return self.objects[key]

    async def delete(self, tenant_id: str, key: str) -> None:
        assert_key_owned_by(key, tenant_id)
        self.deleted.append(key)
        self.objects.pop(key, None)


class _Seeded:
    """Identifiers for the seeded fixture graph (two tenants, three users)."""

    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice_email: str,
        bob_email: str,
        carol_email: str,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_email = alice_email  # tenant A owner under test
        self.bob_email = bob_email  # tenant A, *other* owner
        self.carol_email = carol_email  # tenant B owner


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A StaticPool SQLite engine + schema; seed two tenants and three users."""
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
        factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
            tenant_a=tenant_a.id,
            tenant_b=tenant_b.id,
            alice_email="alice@acme.test",
            bob_email="bob@acme.test",
            carol_email="carol@globex.test",
        )
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def store() -> FakeObjectStore:
    return FakeObjectStore()


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession], store: FakeObjectStore
) -> Iterator[FastAPI]:
    """The app with its DB + object-store dependencies pointed at fakes.

    Overriding ``get_object_store_dep`` keeps the delete route's #269 object
    cleanup off real MinIO (offline-safe).
    """
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_object_store_dep] = lambda: store
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


async def _seed_collection(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    owner_email: str,
    name: str = "Seeded",
    documents: int = 0,
) -> uuid.UUID:
    """Insert a collection (and ``documents`` docs) directly, return its id."""
    async with sessionmaker() as session:
        owner = await UserRepository(session, tenant_id).get_by_email(owner_email)
        assert owner is not None
        coll = await CollectionRepository(session, tenant_id).create(owner_id=owner.id, name=name)
        for i in range(documents):
            await DocumentRepository(session, tenant_id).create(
                owner_id=owner.id,
                collection_id=coll.id,
                filename=f"doc-{i}.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key=f"{tenant_id}/k-{i}",
                acl_enforced=False,
            )
        await session.commit()
        return coll.id


# --- Happy path -------------------------------------------------------------


async def test_create_collection_returns_201_with_owner_and_counts(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/collections",
        headers=_auth(token),
        json={"name": "Q3 Docs", "description": "quarter three"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {
        "id",
        "name",
        "description",
        "owner_id",
        "document_count",
        "created_at",
        "updated_at",
    }
    assert body["name"] == "Q3 Docs"
    assert body["description"] == "quarter three"
    assert body["document_count"] == 0
    # owner_id is the caller, from the token (never request input).
    me = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert body["owner_id"] == me.json()["id"]


async def test_create_without_description_omits_it(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post("/api/v1/collections", headers=_auth(token), json={"name": "No desc"})
    assert resp.status_code == 201, resp.text
    assert "description" not in resp.json()


async def test_get_collection_reflects_document_count(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email, documents=3
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/collections/{coll_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["document_count"] == 3


async def test_list_returns_only_callers_collections(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email, name="mine"
    )
    # Another owner in the same tenant — must NOT appear in Alice's list.
    await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.bob_email, name="bobs"
    )
    # Another tenant entirely — must NOT appear either.
    await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email, name="carols"
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/collections", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {item["name"] for item in body["items"]}
    assert names == {"mine"}
    # No further pages: next_cursor is absent (or null) — both are contract-valid
    # (the field is optional/nullable; exhausted pages carry no cursor).
    assert body.get("next_cursor") is None


async def test_list_cursor_pagination_walks_all_pages(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created: set[str] = set()
    for i in range(5):
        r = await client.post("/api/v1/collections", headers=_auth(token), json={"name": f"c{i}"})
        created.add(r.json()["id"])

    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/collections", headers=_auth(token), params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) <= 2
        for item in body["items"]:
            assert item["id"] not in seen  # no duplicates across pages
            seen.add(item["id"])
        pages += 1
        cursor = body.get("next_cursor")
        if cursor is None:
            break
        assert pages < 10  # guard against a non-terminating cursor
    assert seen == created


async def test_patch_updates_name_and_description(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post("/api/v1/collections", headers=_auth(token), json={"name": "old"})
    coll_id = created.json()["id"]
    resp = await client.patch(
        f"/api/v1/collections/{coll_id}",
        headers=_auth(token),
        json={"name": "new", "description": "added"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "new"
    assert body["description"] == "added"


async def test_delete_removes_collection_and_cascades(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email, documents=2
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.delete(f"/api/v1/collections/{coll_id}", headers=_auth(token))
    assert resp.status_code == 204
    # Gone for the owner now.
    gone = await client.get(f"/api/v1/collections/{coll_id}", headers=_auth(token))
    assert gone.status_code == 404
    # Cascade: its documents are gone too.
    async with sessionmaker() as session:
        docs = await DocumentRepository(session, seeded.tenant_a).list_in_collection(coll_id)
        assert docs == []


# --- Audit emission (INV-6, spec 0004 §2.4) --------------------------------


async def test_create_emits_collection_created_audit(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/collections",
        headers=_auth(token),
        json={"name": "audited"},
    )
    coll_id = created.json()["id"]

    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    created_ev = next(e for e in events if e.action == "collection.created")
    assert created_ev.resource_id == coll_id
    assert created_ev.resource_type == "collection"
    assert created_ev.outcome.value == "allowed"


async def test_delete_emits_document_deleted_for_each_cascaded_doc(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # The audit taxonomy (spec 0004 §2.4) defines document.deleted, not a
    # collection-level delete event; the cascade audits each removed document.
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email, documents=2
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.delete(f"/api/v1/collections/{coll_id}", headers=_auth(token))
    assert resp.status_code == 204

    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    deleted = [e for e in events if e.action == "document.deleted"]
    assert len(deleted) == 2
    assert all(e.resource_type == "document" for e in deleted)
    assert all(e.metadata.get("collection_id") == str(coll_id) for e in deleted)


# --- Negative: tenancy / ownership (INV-1/INV-2 → 404, never 403) ----------


async def test_get_other_owner_same_tenant_is_404(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Bob's collection in the same tenant — Alice must get 404 (not 403).
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.bob_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/collections/{coll_id}", headers=_auth(token))
    assert resp.status_code == 404


async def test_get_cross_tenant_is_404(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/collections/{coll_id}", headers=_auth(token))
    assert resp.status_code == 404


async def test_patch_other_owner_is_404(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.bob_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.patch(
        f"/api/v1/collections/{coll_id}", headers=_auth(token), json={"name": "hijack"}
    )
    assert resp.status_code == 404


async def test_delete_cross_tenant_is_404_and_leaves_row(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.delete(f"/api/v1/collections/{coll_id}", headers=_auth(token))
    assert resp.status_code == 404
    # The owning tenant's row is untouched.
    async with sessionmaker() as session:
        still = await CollectionRepository(session, seeded.tenant_b).get(coll_id)
        assert still is not None


async def test_get_unknown_id_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/collections/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404


# --- Negative: authentication (INV-4 → 401) --------------------------------


async def test_list_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/collections")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_create_with_malformed_token_is_401(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/collections",
        headers={"Authorization": "Bearer not.a.jwt"},
        json={"name": "x"},
    )
    assert resp.status_code == 401


# --- Negative: malformed body (INV-8 → 422) --------------------------------


async def test_create_empty_name_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post("/api/v1/collections", headers=_auth(token), json={"name": ""})
    assert resp.status_code == 422


async def test_create_missing_name_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post("/api/v1/collections", headers=_auth(token), json={})
    assert resp.status_code == 422


async def test_create_name_too_long_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post("/api/v1/collections", headers=_auth(token), json={"name": "x" * 201})
    assert resp.status_code == 422


async def test_create_unknown_field_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/collections",
        headers=_auth(token),
        json={"name": "ok", "owner_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


async def test_patch_empty_body_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post("/api/v1/collections", headers=_auth(token), json={"name": "c"})
    coll_id = created.json()["id"]
    resp = await client.patch(f"/api/v1/collections/{coll_id}", headers=_auth(token), json={})
    assert resp.status_code == 422


async def test_get_malformed_uuid_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/collections/not-a-uuid", headers=_auth(token))
    assert resp.status_code == 422
