"""User avatar API tests — PUT/DELETE /me/avatar + /auth/me avatar_url (user settings).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite DB
(mirrors test_admin_api's branding tests), with the object store pointed at an
in-memory fake. Two tenants with two users each are seeded so the per-user +
tenant-scoping negatives are real.

The avatar surface is a PER-USER account write (NOT admin-gated): a user manages
their own profile picture.
* PUT /me/avatar — upload (200 + { avatar_url }); over-size → 413, non-image → 415;
* DELETE /me/avatar — clear (204), idempotent;
* GET /auth/me — avatar_url null before, the presigned URL after;
* isolation: one user's avatar never appears for another (same or other tenant);
* audit: exactly one user.avatar_updated event per write (INV-6);
* auth: no/empty token → 401 (INV-4).
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
from app.db.base import Base
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata
from app.storage.keys import assert_key_owned_by, build_avatar_key
from app.storage.object_store import StoredObject
from app.storage.validation import validate_upload

_PASSWORD = "devpassword"

# A minimal valid 1x1 PNG (declared image/png so the allowlist accepts it).
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f1e0000000049454e44ae426082"
)


class _Seeded:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.alice_email = "alice@acme.test"
        self.bob_email = "bob@acme.test"
        self.carol_email = "carol@globex.test"


class FakeObjectStore:
    """In-memory stand-in for the #22 ``ObjectStore`` — the avatar + /auth/me surface.

    Implements ``put_avatar`` (validates against the real logo/avatar allowlist/limit
    via ``validate_upload``, so 413/415 are exercised end-to-end) and the generic
    ``presign_get`` (tenant-prefix seam enforced by the real ``assert_key_owned_by``).
    Records puts so a test can assert the object was stored under the right prefix.
    """

    def __init__(self) -> None:
        from app.core.config import get_settings

        self._settings = get_settings()
        self.objects: dict[str, bytes] = {}

    async def put_avatar(
        self, tenant_id: str, user_id: str, data: bytes, content_type: str, filename: str
    ) -> StoredObject:
        validate_upload(
            size_bytes=len(data),
            content_type=content_type,
            allowed_content_types=self._settings.logo_allowed_content_types,
            max_bytes=self._settings.max_logo_bytes,
        )
        key = build_avatar_key(tenant_id, user_id, data, filename)
        self.objects[key] = data
        return StoredObject(
            key=key, sha256=key.split("/")[2], size_bytes=len(data), content_type=content_type
        )

    async def presign_get(self, tenant_id: str, key: str) -> str:
        assert_key_owned_by(key, tenant_id)
        return f"https://storage.test/{key}?sig=fake"


@pytest.fixture
def store() -> FakeObjectStore:
    return FakeObjectStore()


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
            await UserRepository(seed, tb.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                tenant_a=ta.id, tenant_b=tb.id, alice_id=alice.id, bob_id=bob.id
            )
            yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession], store: FakeObjectStore
) -> Iterator[FastAPI]:
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


def _avatar_file(
    data: bytes = _PNG_1X1, name: str = "me.png", content_type: str = "image/png"
) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, content_type)}


# --- Upload + read-back -----------------------------------------------------


async def test_put_avatar_uploads_and_returns_url(
    client: AsyncClient, seeded: _Seeded, store: FakeObjectStore
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.put("/api/v1/me/avatar", headers=_auth(token), files=_avatar_file())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"avatar_url"}
    assert isinstance(body["avatar_url"], str) and body["avatar_url"]
    # Stored under tenant A / alice's user-id prefix (the isolation + ownership seam).
    assert any(
        key.startswith(f"{seeded.tenant_a}/{seeded.alice_id}/") for key in store.objects
    )


async def test_me_returns_avatar_url_null_before_and_set_after(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    before = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert before.status_code == 200, before.text
    assert "avatar_url" in before.json()
    assert before.json()["avatar_url"] is None

    put = await client.put("/api/v1/me/avatar", headers=_auth(token), files=_avatar_file())
    assert put.status_code == 200, put.text
    after = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert after.json()["avatar_url"] is not None
    assert isinstance(after.json()["avatar_url"], str)


async def test_delete_avatar_clears(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    await client.put("/api/v1/me/avatar", headers=_auth(token), files=_avatar_file())
    resp = await client.delete("/api/v1/me/avatar", headers=_auth(token))
    assert resp.status_code == 204, resp.text
    me = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.json()["avatar_url"] is None


async def test_delete_avatar_is_idempotent(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.delete("/api/v1/me/avatar", headers=_auth(token))
    assert resp.status_code == 204, resp.text


# --- Negatives (INV-8) ------------------------------------------------------


async def test_put_avatar_rejects_non_image_415(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.put(
        "/api/v1/me/avatar",
        headers=_auth(token),
        files=_avatar_file(data=b"not an image", name="notes.txt", content_type="text/plain"),
    )
    assert resp.status_code == 415, resp.text


async def test_put_avatar_rejects_oversize_413(client: AsyncClient, seeded: _Seeded) -> None:
    from app.core.config import get_settings

    token = await _login(client, seeded.alice_email)
    oversize = b"\x00" * (get_settings().max_logo_bytes + 1)
    resp = await client.put(
        "/api/v1/me/avatar",
        headers=_auth(token),
        files=_avatar_file(data=oversize, name="huge.png", content_type="image/png"),
    )
    assert resp.status_code == 413, resp.text


# --- Auth + isolation (INV-1/INV-2/INV-4) -----------------------------------


async def test_avatar_unauthenticated_is_401(client: AsyncClient) -> None:
    put = await client.put("/api/v1/me/avatar", files=_avatar_file())
    assert put.status_code == 401
    delete = await client.delete("/api/v1/me/avatar")
    assert delete.status_code == 401


async def test_avatar_is_per_user_same_and_other_tenant(
    client: AsyncClient, seeded: _Seeded
) -> None:
    # Alice uploads an avatar; Bob (same tenant) and Carol (other tenant) are untouched.
    alice = await _login(client, seeded.alice_email)
    await client.put("/api/v1/me/avatar", headers=_auth(alice), files=_avatar_file())
    for email in (seeded.bob_email, seeded.carol_email):
        other = await _login(client, email)
        me = await client.get("/api/v1/auth/me", headers=_auth(other))
        assert me.json()["avatar_url"] is None


async def test_one_user_cannot_affect_anothers_avatar(
    client: AsyncClient, seeded: _Seeded
) -> None:
    # Bob clearing his own avatar leaves Alice's avatar intact (each acts on their own).
    alice = await _login(client, seeded.alice_email)
    await client.put("/api/v1/me/avatar", headers=_auth(alice), files=_avatar_file())
    bob = await _login(client, seeded.bob_email)
    cleared = await client.delete("/api/v1/me/avatar", headers=_auth(bob))
    assert cleared.status_code == 204
    # Alice still has her avatar.
    me = await client.get("/api/v1/auth/me", headers=_auth(alice))
    assert me.json()["avatar_url"] is not None


# --- Audit (INV-6) ----------------------------------------------------------


async def test_put_avatar_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.db.repositories import AuditEventRepository

    token = await _login(client, seeded.alice_email)
    resp = await client.put("/api/v1/me/avatar", headers=_auth(token), files=_avatar_file())
    assert resp.status_code == 200, resp.text
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    avatar_events = [e for e in events if e.action == "user.avatar_updated"]
    assert len(avatar_events) == 1
    ev = avatar_events[0]
    assert ev.metadata["has_avatar"] is True
    assert ev.resource_type == "user"
    assert ev.resource_id == str(seeded.alice_id)
