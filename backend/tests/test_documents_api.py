"""Documents API tests — the /documents contract + required negatives (#28).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite
database (no Postgres) and an **in-memory fake object store** (no MinIO), so the
whole suite stays green offline (object-store-dependent paths are faked, not
skipped). Mirrors ``test_collections_api``: ``get_db_session`` is overridden to a
StaticPool SQLite engine seeded with two tenants and three users;
``get_object_store_dep`` is overridden with a fake that records put/get/delete/
presign per content-addressed key, so upload → content → delete round-trips
exercise the real service/router wiring without a live S3.

Covered:

* happy path: upload (201, status=pending, chunk_count=0) → get → list (filters +
  cursor) → content (302 presigned / inline) → delete (row + chunks + object);
* audit events (``document.uploaded`` / ``downloaded`` / ``deleted``);
* negatives (spec 0004 §3 / the issue's required set):
  - INV-1/INV-2: another tenant's or another user's document on GET / content /
    DELETE → **404** (never 403, existence non-disclosure); upload into a
    foreign / other-owner collection → **404**;
  - INV-4: no / malformed bearer → 401;
  - over-size upload → **413**; unsupported content-type → **415**;
  - INV-8: missing multipart field(s) → **422**.
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
    ChunkRepository,
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
_TXT = "text/plain"


# --- In-memory fake object store (offline-safe; mirrors ObjectStore surface) -


class FakeObjectStore:
    """An in-memory stand-in for the #22 ``ObjectStore`` (no MinIO needed).

    Honours the same contract the service relies on: tenant-prefixed,
    content-addressed keys; the tenant-prefix seam on get/delete/presign
    (cross-prefix → ForbiddenError, via the real ``assert_key_owned_by``); a
    missing get → ``NotFoundError``. Records every call so tests can assert the
    object lifecycle without a live S3.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.presigned: list[str] = []

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

    async def presign_get(self, tenant_id: str, key: str) -> str:
        assert_key_owned_by(key, tenant_id)
        url = f"https://storage.test/{key}?sig=fake"
        self.presigned.append(url)
        return url


# --- Seeding ---------------------------------------------------------------


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
    """The app with its DB + object-store dependencies pointed at fakes."""
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


# --- Helpers ----------------------------------------------------------------


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
) -> uuid.UUID:
    async with sessionmaker() as session:
        owner = await UserRepository(session, tenant_id).get_by_email(owner_email)
        assert owner is not None
        coll = await CollectionRepository(session, tenant_id).create(owner_id=owner.id, name=name)
        await session.commit()
        return coll.id


async def _seed_document(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
    *,
    tenant_id: uuid.UUID,
    owner_email: str,
    collection_id: uuid.UUID,
    filename: str = "seed.txt",
    data: bytes = b"seed body",
) -> uuid.UUID:
    """Insert a document directly (with its object in the fake store), return id."""
    stored = await store.put(str(tenant_id), data, _TXT, filename)
    async with sessionmaker() as session:
        owner = await UserRepository(session, tenant_id).get_by_email(owner_email)
        assert owner is not None
        doc = await DocumentRepository(session, tenant_id).create(
            owner_id=owner.id,
            collection_id=collection_id,
            filename=filename,
            mime_type=_TXT,
            size_bytes=len(data),
            storage_key=stored.key,
        )
        await session.commit()
        return doc.id


def _upload_files(filename: str = "report.txt", data: bytes = b"hello", ctype: str = _TXT) -> dict:
    return {"file": (filename, data, ctype)}


# --- Happy path: upload ----------------------------------------------------


async def test_upload_returns_201_pending_owned_by_caller(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(coll_id)},
        files=_upload_files(),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) >= {
        "id",
        "filename",
        "mime_type",
        "size_bytes",
        "collection_id",
        "owner_id",
        "status",
        "chunk_count",
        "created_at",
        "updated_at",
    }
    assert body["filename"] == "report.txt"
    assert body["mime_type"] == _TXT
    assert body["size_bytes"] == len(b"hello")
    assert body["collection_id"] == str(coll_id)
    assert body["status"] == "pending"  # ingestion (#21) not run here
    assert body["chunk_count"] == 0
    me = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert body["owner_id"] == me.json()["id"]


async def test_upload_stores_object_in_object_store(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(coll_id)},
        files=_upload_files(data=b"the body bytes"),
    )
    assert resp.status_code == 201, resp.text
    # The bytes landed under the caller's tenant prefix (the #22 seam).
    assert len(store.objects) == 1
    key = next(iter(store.objects))
    assert key.startswith(f"{seeded.tenant_a}/")
    assert store.objects[key] == b"the body bytes"


# --- Happy path: list / get -------------------------------------------------


async def test_list_returns_only_callers_documents(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll_a = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email, name="alice"
    )
    coll_bob = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.bob_email, name="bob"
    )
    coll_carol = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email, name="carol"
    )
    await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll_a,
        filename="mine.txt",
        data=b"a",
    )
    await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.bob_email,
        collection_id=coll_bob,
        filename="bobs.txt",
        data=b"b",
    )
    await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_b,
        owner_email=seeded.carol_email,
        collection_id=coll_carol,
        filename="carols.txt",
        data=b"c",
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/documents", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    names = {item["filename"] for item in resp.json()["items"]}
    assert names == {"mine.txt"}


async def test_list_filters_by_collection_status_and_q(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll_one = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email, name="one"
    )
    coll_two = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email, name="two"
    )
    await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll_one,
        filename="alpha-report.txt",
        data=b"1",
    )
    await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll_two,
        filename="beta-notes.txt",
        data=b"2",
    )
    token = await _login(client, seeded.alice_email)

    # Filter by collection.
    resp = await client.get(
        "/api/v1/documents", headers=_auth(token), params={"collection_id": str(coll_one)}
    )
    assert {i["filename"] for i in resp.json()["items"]} == {"alpha-report.txt"}

    # Filter by filename substring (lexical).
    resp = await client.get("/api/v1/documents", headers=_auth(token), params={"q": "beta"})
    assert {i["filename"] for i in resp.json()["items"]} == {"beta-notes.txt"}

    # Filter by status — both are pending (no ingestion); 'ready' yields none.
    resp = await client.get("/api/v1/documents", headers=_auth(token), params={"status": "pending"})
    assert len(resp.json()["items"]) == 2
    resp = await client.get("/api/v1/documents", headers=_auth(token), params={"status": "ready"})
    assert resp.json()["items"] == []


async def test_list_cursor_pagination_walks_all_pages(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    created: set[str] = set()
    for i in range(5):
        doc_id = await _seed_document(
            sessionmaker,
            store,
            tenant_id=seeded.tenant_a,
            owner_email=seeded.alice_email,
            collection_id=coll,
            filename=f"d{i}.txt",
            data=f"body-{i}".encode(),
        )
        created.add(str(doc_id))
    token = await _login(client, seeded.alice_email)

    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/documents", headers=_auth(token), params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) <= 2
        for item in body["items"]:
            assert item["id"] not in seen
            seen.add(item["id"])
        pages += 1
        cursor = body.get("next_cursor")
        if cursor is None:
            break
        assert pages < 10
    assert seen == created


async def test_get_document_returns_metadata(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(doc_id)
    assert resp.json()["status"] == "pending"


# --- Happy path: content (302 presigned default + inline) ------------------


async def test_content_redirects_302_to_presigned_url(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(
        f"/api/v1/documents/{doc_id}/content", headers=_auth(token), follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://storage.test/")
    assert store.presigned  # the #22 presign_get path was used


async def test_content_streams_inline_when_redirect_disabled(
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flip the config flag so the API streams the bytes inline instead of 302.
    monkeypatch.setenv("DOCUMENT_CONTENT_REDIRECT", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        coll = await _seed_collection(
            sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
        )
        doc_id = await _seed_document(
            sessionmaker,
            store,
            tenant_id=seeded.tenant_a,
            owner_email=seeded.alice_email,
            collection_id=coll,
            data=b"inline bytes here",
        )
        application = create_app()

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with sessionmaker() as session:
                yield session

        application.dependency_overrides[get_db_session] = _override_session
        application.dependency_overrides[get_object_store_dep] = lambda: store
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            token = await _login(ac, seeded.alice_email)
            resp = await ac.get(f"/api/v1/documents/{doc_id}/content", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.content == b"inline bytes here"
    finally:
        monkeypatch.delenv("DOCUMENT_CONTENT_REDIRECT", raising=False)
        get_settings.cache_clear()


# --- Happy path: delete -----------------------------------------------------


async def test_delete_removes_row_chunks_and_object(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll,
    )
    # Give it a chunk so the cascade is observable.
    async with sessionmaker() as session:
        await ChunkRepository(session, seeded.tenant_a).add(
            document_id=doc_id, ord=0, text="hi", char_start=0, char_end=2
        )
        await session.commit()
    token = await _login(client, seeded.alice_email)
    resp = await client.delete(f"/api/v1/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 204
    # Gone for the owner.
    gone = await client.get(f"/api/v1/documents/{doc_id}", headers=_auth(token))
    assert gone.status_code == 404
    # The stored object was removed via the #22 adapter.
    assert store.objects == {}
    assert store.deleted
    # Cascade: its chunks are gone too.
    async with sessionmaker() as session:
        chunks = await ChunkRepository(session, seeded.tenant_a).list_for_document(doc_id)
        assert chunks == []


# --- Audit emission (INV-6, spec 0004 §2.4) --------------------------------


async def test_upload_emits_document_uploaded_audit(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(coll_id)},
        files=_upload_files(),
    )
    doc_id = created.json()["id"]
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    ev = next(e for e in events if e.action == "document.uploaded")
    assert ev.resource_id == doc_id
    assert ev.resource_type == "document"
    assert ev.outcome.value == "allowed"


async def test_content_emits_document_downloaded_audit(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    await client.get(
        f"/api/v1/documents/{doc_id}/content", headers=_auth(token), follow_redirects=False
    )
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    assert any(e.action == "document.downloaded" and e.resource_id == str(doc_id) for e in events)


async def test_delete_emits_document_deleted_audit(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    await client.delete(f"/api/v1/documents/{doc_id}", headers=_auth(token))
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    assert any(e.action == "document.deleted" and e.resource_id == str(doc_id) for e in events)


# --- Negative: tenancy / ownership (INV-1/INV-2 → 404, never 403) ----------


async def test_get_other_owner_same_tenant_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.bob_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.bob_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 404


async def test_get_cross_tenant_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_b,
        owner_email=seeded.carol_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 404


async def test_content_other_owner_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.bob_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.bob_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(
        f"/api/v1/documents/{doc_id}/content", headers=_auth(token), follow_redirects=False
    )
    assert resp.status_code == 404


async def test_delete_cross_tenant_is_404_and_leaves_row_and_object(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email
    )
    doc_id = await _seed_document(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_b,
        owner_email=seeded.carol_email,
        collection_id=coll,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.delete(f"/api/v1/documents/{doc_id}", headers=_auth(token))
    assert resp.status_code == 404
    # The owning tenant's row and object are untouched.
    async with sessionmaker() as session:
        still = await DocumentRepository(session, seeded.tenant_b).get(doc_id)
        assert still is not None
    assert store.objects  # object not deleted
    assert store.deleted == []


async def test_upload_into_other_owner_collection_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    # Bob's collection in the same tenant — Alice may not upload into it.
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.bob_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(coll)},
        files=_upload_files(),
    )
    assert resp.status_code == 404
    # Nothing stored, no row created.
    assert store.objects == {}


async def test_upload_into_cross_tenant_collection_is_404(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_b, owner_email=seeded.carol_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(coll)},
        files=_upload_files(),
    )
    assert resp.status_code == 404


async def test_upload_unknown_collection_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(uuid.uuid4())},
        files=_upload_files(),
    )
    assert resp.status_code == 404


# --- Negative: authentication (INV-4 → 401) --------------------------------


async def test_list_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/documents")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_upload_with_malformed_token_is_401(client: AsyncClient, seeded: _Seeded) -> None:
    resp = await client.post(
        "/api/v1/documents",
        headers={"Authorization": "Bearer not.a.jwt"},
        data={"collection_id": str(uuid.uuid4())},
        files=_upload_files(),
    )
    assert resp.status_code == 401


# --- Negative: upload validation (413 / 415) -------------------------------


async def test_upload_oversize_is_413(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shrink the cap so a tiny payload trips it, rebuild the app under that env.
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        coll_id = await _seed_collection(
            sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
        )
        application = create_app()

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with sessionmaker() as session:
                yield session

        application.dependency_overrides[get_db_session] = _override_session
        application.dependency_overrides[get_object_store_dep] = lambda: FakeObjectStore()
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            token = await _login(ac, seeded.alice_email)
            resp = await ac.post(
                "/api/v1/documents",
                headers=_auth(token),
                data={"collection_id": str(coll_id)},
                files=_upload_files(data=b"way too big"),
            )
        assert resp.status_code == 413
    finally:
        monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
        get_settings.cache_clear()


async def test_upload_unsupported_content_type_is_415(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    coll_id = await _seed_collection(
        sessionmaker, tenant_id=seeded.tenant_a, owner_email=seeded.alice_email
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(coll_id)},
        files=_upload_files(filename="evil.zip", data=b"PK\x03\x04", ctype="application/zip"),
    )
    assert resp.status_code == 415


# --- Negative: malformed multipart (INV-8 → 422) ---------------------------


async def test_upload_missing_file_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422


async def test_upload_missing_collection_id_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        files=_upload_files(),
    )
    assert resp.status_code == 422


async def test_upload_non_uuid_collection_id_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/documents",
        headers=_auth(token),
        data={"collection_id": "not-a-uuid"},
        files=_upload_files(),
    )
    assert resp.status_code == 422


async def test_get_malformed_uuid_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/documents/not-a-uuid", headers=_auth(token))
    assert resp.status_code == 422
