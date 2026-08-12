"""Artifacts API tests — the /artifacts read/delete contract + negatives (#222).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite
database (no Postgres) and an **in-memory fake object store** (no MinIO), so the
whole suite stays green offline (object-store-dependent paths are faked, not
skipped). Mirrors ``test_documents_api``: ``get_db_session`` is overridden to a
StaticPool SQLite engine seeded with two tenants and three users;
``get_object_store_dep`` is overridden with a fake that records the artifact
lifecycle per content-addressed key, so list → get → content → delete round-trips
exercise the real service/router wiring without a live S3.

Artifacts are **produced** by the file-writing tool / code sandbox off the request
path (there is no upload/create endpoint here), so the fixtures seed rows directly
via ``ArtifactsService.create_artifact`` against the fake store.

Covered:

* happy path: list (owner-scoped, filters, cursor) → get → content (302 presigned
  / inline attachment) → delete (object + row);
* audit events (``artifact.downloaded`` / ``artifact.deleted``);
* negatives (spec 0004 §3 / the issue's required set):
  - INV-1/INV-2: another tenant's or another user's artifact on GET / content /
    DELETE → **404** (never 403, existence non-disclosure); a non-owner sees only
    their own artifacts on list;
  - INV-4: no / malformed bearer → 401;
  - INV-8: malformed (non-uuid) id → **422**.
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
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import ArtifactProducedBy, Role
from app.main import create_app
from app.services.artifacts_service import ArtifactLinks, ArtifactsService
from app.services.audit import AuditSink
from app.storage.keys import assert_artifact_key_owned_by, build_artifact_key
from app.storage.object_store import StoredObject

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PASSWORD = "devpassword"
_TXT = "text/plain"
_CSV = "text/csv"


# --- In-memory fake object store (offline-safe; artifact surface) ----------


class FakeObjectStore:
    """An in-memory stand-in for the #22 ``ObjectStore`` artifact lifecycle.

    Honours the contract the service relies on: tenant-prefixed, content-addressed
    ``artifacts/`` keys; the artifact tenant-prefix seam on get/delete/presign
    (cross-prefix → ForbiddenError, via the real ``assert_artifact_key_owned_by``);
    a missing get → ``NotFoundError``. Records every call so tests can assert the
    object lifecycle without a live S3.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.presigned: list[str] = []

    async def put_artifact(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> StoredObject:
        key = build_artifact_key(tenant_id, data, filename)
        self.objects[key] = data
        return StoredObject(
            key=key, sha256=key.split("/")[2], size_bytes=len(data), content_type=content_type
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        assert_artifact_key_owned_by(key, tenant_id)
        if key not in self.objects:
            raise NotFoundError("object not found", code="object_not_found")
        return self.objects[key]

    async def delete_artifact(self, tenant_id: str, key: str) -> None:
        assert_artifact_key_owned_by(key, tenant_id)
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def presign_get_artifact(self, tenant_id: str, key: str) -> str:
        assert_artifact_key_owned_by(key, tenant_id)
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
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
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


async def _seed_artifact(
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
    *,
    tenant_id: uuid.UUID,
    owner_email: str,
    filename: str = "out.txt",
    content_type: str = _TXT,
    data: bytes = b"artifact body",
    produced_by: ArtifactProducedBy = ArtifactProducedBy.TOOL,
    session_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Create an artifact via the real service (bytes in the fake store), return id.

    Goes through ``ArtifactsService.create_artifact`` — the same seam the
    file-writing tool / sandbox use — so the row and its content-addressed object
    are produced exactly as production would. There is no create *endpoint*, so
    this is how a produced artifact enters the system under test.
    """
    async with sessionmaker() as session:
        owner = await UserRepository(session, tenant_id).get_by_email(owner_email)
        assert owner is not None
        settings = get_settings()
        service = ArtifactsService(
            session,
            tenant_id=tenant_id,
            owner_id=owner.id,
            object_store=store,  # type: ignore[arg-type]  # the fake honours the surface
            audit=AuditSink(AuditEventRepository(session, tenant_id)),
            denials=None,  # seed helper is create-only
            request_id="seed",
            source_ip="127.0.0.1",
            artifact_allowed_content_types=settings.artifact_allowed_content_types,
            max_artifact_bytes=settings.max_artifact_bytes,
        )
        artifact = await service.create_artifact(
            data=data,
            filename=filename,
            content_type=content_type,
            produced_by=produced_by,
            links=ArtifactLinks(session_id=session_id) if session_id else None,
        )
        await session.commit()
        return artifact.id


# --- Happy path: list / get -------------------------------------------------


async def test_list_returns_only_callers_artifacts(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        filename="mine.txt",
        data=b"a",
    )
    await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.bob_email,
        filename="bobs.txt",
        data=b"b",
    )
    await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_b,
        owner_email=seeded.carol_email,
        filename="carols.txt",
        data=b"c",
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/artifacts", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    names = {item["filename"] for item in resp.json()["items"]}
    assert names == {"mine.txt"}


async def test_list_filters_by_session_and_produced_by(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        filename="from-tool.csv",
        content_type=_CSV,
        data=b"1,2,3",
        produced_by=ArtifactProducedBy.TOOL,
    )
    await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        filename="from-run.txt",
        data=b"run-output",
        produced_by=ArtifactProducedBy.RUN,
    )
    token = await _login(client, seeded.alice_email)

    # Filter by produced_by.
    resp = await client.get(
        "/api/v1/artifacts", headers=_auth(token), params={"produced_by": "tool"}
    )
    assert {i["filename"] for i in resp.json()["items"]} == {"from-tool.csv"}
    resp = await client.get(
        "/api/v1/artifacts", headers=_auth(token), params={"produced_by": "run"}
    )
    assert {i["filename"] for i in resp.json()["items"]} == {"from-run.txt"}

    # Filter by session_id — none carry a session, so it yields nothing.
    resp = await client.get(
        "/api/v1/artifacts", headers=_auth(token), params={"session_id": str(uuid.uuid4())}
    )
    assert resp.json()["items"] == []


async def test_list_cursor_pagination_walks_all_pages(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    created: set[str] = set()
    for i in range(5):
        art_id = await _seed_artifact(
            sessionmaker,
            store,
            tenant_id=seeded.tenant_a,
            owner_email=seeded.alice_email,
            filename=f"a{i}.txt",
            data=f"body-{i}".encode(),
        )
        created.add(str(art_id))
    token = await _login(client, seeded.alice_email)

    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/artifacts", headers=_auth(token), params=params)
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


async def test_get_artifact_returns_metadata(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        filename="deliverable.csv",
        content_type=_CSV,
        data=b"a,b\n1,2",
        produced_by=ArtifactProducedBy.TOOL,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/artifacts/{art_id}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(art_id)
    assert body["filename"] == "deliverable.csv"
    assert body["mime_type"] == _CSV
    assert body["produced_by"] == "tool"
    assert body["size_bytes"] == len(b"a,b\n1,2")
    me = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert body["owner_id"] == me.json()["id"]


# --- Happy path: content (302 presigned default + inline) ------------------


async def test_content_redirects_302_to_presigned_url(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(
        f"/api/v1/artifacts/{art_id}/content", headers=_auth(token), follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://storage.test/")
    assert store.presigned  # the #22 presign_get_artifact path was used


async def test_content_streams_inline_when_redirect_disabled(
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCUMENT_CONTENT_REDIRECT", "false")
    get_settings.cache_clear()
    try:
        art_id = await _seed_artifact(
            sessionmaker,
            store,
            tenant_id=seeded.tenant_a,
            owner_email=seeded.alice_email,
            data=b"inline artifact bytes",
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
            resp = await ac.get(f"/api/v1/artifacts/{art_id}/content", headers=_auth(token))
        assert resp.status_code == 200
        # Attachment disposition + nosniff: an artifact (which may be svg/html) is
        # never rendered inline as an active document from the API origin.
        assert resp.headers["content-disposition"].startswith("attachment")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.content == b"inline artifact bytes"
    finally:
        monkeypatch.delenv("DOCUMENT_CONTENT_REDIRECT", raising=False)
        get_settings.cache_clear()


# --- Happy path: delete -----------------------------------------------------


async def test_delete_removes_row_and_object(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
    )
    assert len(store.objects) == 1
    token = await _login(client, seeded.alice_email)
    resp = await client.delete(f"/api/v1/artifacts/{art_id}", headers=_auth(token))
    assert resp.status_code == 204
    # Gone for the owner.
    gone = await client.get(f"/api/v1/artifacts/{art_id}", headers=_auth(token))
    assert gone.status_code == 404
    # The stored object was removed via the #22 adapter.
    assert store.objects == {}
    assert store.deleted


# --- Audit emission (INV-6, spec 0004 §2.4) --------------------------------


async def test_content_emits_artifact_downloaded_audit(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
    )
    token = await _login(client, seeded.alice_email)
    await client.get(
        f"/api/v1/artifacts/{art_id}/content", headers=_auth(token), follow_redirects=False
    )
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    assert any(e.action == "artifact.downloaded" and e.resource_id == str(art_id) for e in events)


async def test_delete_emits_artifact_deleted_audit(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
    )
    token = await _login(client, seeded.alice_email)
    await client.delete(f"/api/v1/artifacts/{art_id}", headers=_auth(token))
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    assert any(e.action == "artifact.deleted" and e.resource_id == str(art_id) for e in events)


# --- Negative: tenancy / ownership (INV-1/INV-2 → 404, never 403) ----------


async def test_get_other_owner_same_tenant_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
    durable_audit_ledger,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.bob_email,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/artifacts/{art_id}", headers=_auth(token))
    assert resp.status_code == 404
    assert len(durable_audit_ledger.events) == 1
    assert durable_audit_ledger.events[0].metadata == {
        "attempted_action": "artifact.read",
        "reason": "not_visible",
    }


async def test_get_cross_tenant_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_b,
        owner_email=seeded.carol_email,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/artifacts/{art_id}", headers=_auth(token))
    assert resp.status_code == 404


async def test_content_other_owner_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.bob_email,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.get(
        f"/api/v1/artifacts/{art_id}/content", headers=_auth(token), follow_redirects=False
    )
    assert resp.status_code == 404


async def test_delete_cross_tenant_is_404_and_leaves_row_and_object(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    art_id = await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_b,
        owner_email=seeded.carol_email,
    )
    token = await _login(client, seeded.alice_email)
    resp = await client.delete(f"/api/v1/artifacts/{art_id}", headers=_auth(token))
    assert resp.status_code == 404
    # The owning tenant's row and object are untouched.
    from app.db.repositories import ArtifactRepository

    async with sessionmaker() as session:
        still = await ArtifactRepository(session, seeded.tenant_b).get(art_id)
        assert still is not None
    assert store.objects  # object not deleted
    assert store.deleted == []


async def test_list_excludes_other_owners_for_member(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    store: FakeObjectStore,
) -> None:
    """A member sees only their own artifacts — never another owner's (INV-2)."""
    await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.alice_email,
        filename="alice.txt",
        data=b"alice",
    )
    await _seed_artifact(
        sessionmaker,
        store,
        tenant_id=seeded.tenant_a,
        owner_email=seeded.bob_email,
        filename="bob.txt",
        data=b"bob",
    )
    # Bob lists — sees only his own, not Alice's.
    token = await _login(client, seeded.bob_email)
    resp = await client.get("/api/v1/artifacts", headers=_auth(token))
    assert resp.status_code == 200
    assert {i["filename"] for i in resp.json()["items"]} == {"bob.txt"}


# --- Negative: authentication (INV-4 → 401) --------------------------------


async def test_list_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/artifacts")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_get_with_malformed_token_is_401(client: AsyncClient, seeded: _Seeded) -> None:
    resp = await client.get(
        f"/api/v1/artifacts/{uuid.uuid4()}",
        headers={"Authorization": "Bearer not.a.jwt"},
    )
    assert resp.status_code == 401


# --- Negative: malformed id (INV-8 → 422) ----------------------------------


async def test_get_malformed_uuid_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/artifacts/not-a-uuid", headers=_auth(token))
    assert resp.status_code == 422


async def test_delete_malformed_uuid_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.delete("/api/v1/artifacts/not-a-uuid", headers=_auth(token))
    assert resp.status_code == 422
