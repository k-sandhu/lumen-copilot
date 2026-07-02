"""Assistants API tests — the frozen /assistants contract, end-to-end (issue #211).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (no
Postgres / Redis / model), mirroring the chat/sources API tests. Covers the frozen
``contracts/openapi.yaml`` ``/assistants*`` surface + the additive ``assistant_id``
on ``POST /chat/sessions``:

* CRUD happy paths + the contract response shapes (camelCase assistant fields);
* publish → 201 + immutable version; versions list; rollback;
* start a chat from an assistant (``POST /chat/sessions`` with ``assistant_id``);
* negatives: unknown tool in the allow-list → 422 (AC-4); publish without a backup
  owner → 422 (AC-1); cross-tenant / other-owner id → 404 (AC-5, INV-1/INV-2);
  no token → 401 (INV-4); a non-published assistant on session create → 422.
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

from app.api.deps import get_backplane_dep, get_db_session
from app.auth import hash_password
from app.db.base import Base
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app
from app.realtime.backplane import InMemoryBackplane

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice: uuid.UUID,
        bob: uuid.UUID,
        carol: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice
        self.bob = bob
        self.carol = carol
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
                email="alice@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
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
                tenant_a=ta.id, tenant_b=tb.id, alice=alice.id, bob=bob.id, carol=carol.id
            )
            yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_backplane_dep] = lambda: InMemoryBackplane()

    import app.main as main_module

    class _NoopStore:
        async def ensure_bucket(self) -> None:
            return None

    monkeypatch.setattr(main_module, "get_object_store", lambda: _NoopStore())
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


# --- CRUD happy path + contract shape --------------------------------------


async def test_create_get_list_update_delete(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)

    created = await client.post(
        "/api/v1/assistants",
        headers=_auth(token),
        json={"name": "Renewals", "instructions": "Be terse."},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    # Contract shape: camelCase assistant fields + snake_case common fields.
    assert body["name"] == "Renewals"
    assert body["owner"] == str(seeded.alice)
    assert body["status"] == "draft"
    assert body["autonomyLevel"] == "suggest"
    assert body["knowledgeScope"] == {"collectionIds": [], "sourceIds": [], "modes": []}
    assert body["toolAllowlist"] == []
    assert "created_at" in body and "updated_at" in body
    assistant_id = body["id"]

    got = await client.get(f"/api/v1/assistants/{assistant_id}", headers=_auth(token))
    assert got.status_code == 200
    assert got.json()["id"] == assistant_id

    listed = await client.get("/api/v1/assistants", headers=_auth(token))
    assert listed.status_code == 200
    assert [a["id"] for a in listed.json()["items"]] == [assistant_id]

    patched = await client.patch(
        f"/api/v1/assistants/{assistant_id}",
        headers=_auth(token),
        json={"name": "Renewals v2", "autonomyLevel": "draft"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renewals v2"
    assert patched.json()["autonomyLevel"] == "draft"

    deleted = await client.delete(f"/api/v1/assistants/{assistant_id}", headers=_auth(token))
    assert deleted.status_code == 204
    gone = await client.get(f"/api/v1/assistants/{assistant_id}", headers=_auth(token))
    assert gone.status_code == 404


# --- AC-4: unknown tool → 422 ----------------------------------------------


async def test_unknown_tool_in_allowlist_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/assistants",
        headers=_auth(token),
        json={"name": "Bad", "toolAllowlist": ["not_a_real_tool"]},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "unknown_tool"


# --- AC-1: publish requires a distinct backup owner → 422 -------------------


async def test_publish_without_backup_owner_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants", headers=_auth(token), json={"name": "No backup"}
    )
    aid = created.json()["id"]
    resp = await client.post(f"/api/v1/assistants/{aid}/publish", headers=_auth(token), json={})
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "backup_owner_required"


async def test_publish_then_versions_then_rollback(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants",
        headers=_auth(token),
        json={
            "name": "Pub",
            "instructions": "v1",
            "backupOwner": str(seeded.bob),
        },
    )
    aid = created.json()["id"]

    published = await client.post(
        f"/api/v1/assistants/{aid}/publish", headers=_auth(token), json={"notes": "first"}
    )
    assert published.status_code == 200, published.text
    v1 = published.json()
    assert v1["version"] == 1
    assert v1["assistant_id"] == aid
    assert v1["config"]["instructions"] == "v1"

    # The head now shows status=published + version=1.
    head = await client.get(f"/api/v1/assistants/{aid}", headers=_auth(token))
    assert head.json()["status"] == "published"
    assert head.json()["version"] == 1

    # Edit + publish a second version, then roll back to v1.
    await client.patch(
        f"/api/v1/assistants/{aid}", headers=_auth(token), json={"instructions": "v2"}
    )
    await client.post(f"/api/v1/assistants/{aid}/publish", headers=_auth(token), json={})

    versions = await client.get(f"/api/v1/assistants/{aid}/versions", headers=_auth(token))
    assert versions.status_code == 200
    assert [v["version"] for v in versions.json()["items"]] == [2, 1]

    rolled = await client.post(
        f"/api/v1/assistants/{aid}/rollback", headers=_auth(token), json={"version": 1}
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"] == 3
    assert rolled.json()["config"]["instructions"] == "v1"


async def test_rollback_unknown_version_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants",
        headers=_auth(token),
        json={"name": "R", "backupOwner": str(seeded.bob)},
    )
    aid = created.json()["id"]
    await client.post(f"/api/v1/assistants/{aid}/publish", headers=_auth(token), json={})
    resp = await client.post(
        f"/api/v1/assistants/{aid}/rollback", headers=_auth(token), json={"version": 99}
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "unknown_version"


# --- AC-5 / INV-1 / INV-2: cross-tenant & non-owner → 404 ------------------


async def test_cross_tenant_get_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    alice_token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants", headers=_auth(alice_token), json={"name": "A-only"}
    )
    aid = created.json()["id"]

    carol_token = await _login(client, seeded.carol_email)
    resp = await client.get(f"/api/v1/assistants/{aid}", headers=_auth(carol_token))
    assert resp.status_code == 404


async def test_non_owner_same_tenant_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    alice_token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants", headers=_auth(alice_token), json={"name": "Alice's"}
    )
    aid = created.json()["id"]

    bob_token = await _login(client, seeded.bob_email)
    resp = await client.get(f"/api/v1/assistants/{aid}", headers=_auth(bob_token))
    assert resp.status_code == 404
    patch = await client.patch(
        f"/api/v1/assistants/{aid}", headers=_auth(bob_token), json={"name": "x"}
    )
    assert patch.status_code == 404


async def test_list_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/assistants")
    assert resp.status_code == 401


# --- Start a chat from an assistant (additive ChatSessionCreate) ------------


async def test_start_chat_from_published_assistant(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants",
        headers=_auth(token),
        json={
            "name": "Chatty",
            # A valid, non-default registry id so a match proves the assistant's
            # frozen model default seeded the session (not the server default).
            "model": "openrouter/openai/gpt-5.5",
            "backupOwner": str(seeded.bob),
        },
    )
    aid = created.json()["id"]
    await client.post(f"/api/v1/assistants/{aid}/publish", headers=_auth(token), json={})

    resp = await client.post(
        "/api/v1/chat/sessions", headers=_auth(token), json={"assistant_id": aid}
    )
    assert resp.status_code == 201, resp.text
    # The session inherits the assistant's frozen model default.
    assert resp.json()["model"] == "openrouter/openai/gpt-5.5"


async def test_start_chat_from_draft_assistant_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants", headers=_auth(token), json={"name": "Draft"}
    )
    aid = created.json()["id"]
    resp = await client.post(
        "/api/v1/chat/sessions", headers=_auth(token), json={"assistant_id": aid}
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "assistant_not_published"


async def test_start_chat_from_cross_tenant_assistant_is_404(
    client: AsyncClient, seeded: _Seeded
) -> None:
    alice_token = await _login(client, seeded.alice_email)
    created = await client.post(
        "/api/v1/assistants",
        headers=_auth(alice_token),
        json={"name": "A", "backupOwner": str(seeded.bob)},
    )
    aid = created.json()["id"]
    await client.post(f"/api/v1/assistants/{aid}/publish", headers=_auth(alice_token), json={})

    carol_token = await _login(client, seeded.carol_email)
    resp = await client.post(
        "/api/v1/chat/sessions", headers=_auth(carol_token), json={"assistant_id": aid}
    )
    assert resp.status_code == 404
