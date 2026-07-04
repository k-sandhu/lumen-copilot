"""Run-deliveries API tests — the frozen ``/run-deliveries`` contract (issue #238).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (no
Postgres / Redis / model), mirroring the runs API tests. Covers the two frozen
routes:

* ``GET /run-deliveries`` — the in-app inbox: the caller's own deliveries (newest
  first), filterable by ``status`` / ``unread``, paginated;
* ``POST /run-deliveries/{deliveryId}/read`` — mark one delivery read (idempotent);

plus the mandatory negatives: a cross-tenant / non-owned delivery id → **404**
(INV-1/INV-2, existence non-disclosure); no/bad token → **401** (INV-4).

Deliveries are seeded directly through the repository (the create path is the run
task on completion, out of these read routes' scope), so the fixture stands up
deliveries in two tenants + a second owner to prove the isolation.
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
from app.db.repositories import (
    AssistantRepository,
    RunDeliveryRepository,
    RunRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AutonomyLevel,
    KnowledgeScope,
    Role,
    RunDeliveryKind,
    RunDeliveryStatus,
    RunTrigger,
)
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        alice_delivery: uuid.UUID,
        alice_delivery_read: uuid.UUID,
        bob_delivery: uuid.UUID,
        carol_delivery: uuid.UUID,
    ) -> None:
        self.alice_email = "alice@acme.test"
        self.bob_email = "bob@acme.test"
        self.carol_email = "carol@globex.test"
        self.alice_delivery = alice_delivery
        self.alice_delivery_read = alice_delivery_read
        self.bob_delivery = bob_delivery
        self.carol_delivery = carol_delivery


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
            a_assistant = await AssistantRepository(seed, ta.id).create(
                owner_id=alice.id, name="A", knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(), autonomy_level=AutonomyLevel.SUGGEST, backup_owner_id=None,
            )
            b_assistant = await AssistantRepository(seed, tb.id).create(
                owner_id=carol.id, name="B", knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(), autonomy_level=AutonomyLevel.SUGGEST, backup_owner_id=None,
            )

            async def _run(tenant_id: uuid.UUID, owner_id: uuid.UUID, assistant_id: uuid.UUID):
                return await RunRepository(seed, tenant_id).create(
                    owner_id=owner_id, assistant_id=assistant_id,
                    assistant_version_id=None, trigger=RunTrigger.MANUAL,
                )

            deliveries_a = RunDeliveryRepository(seed, ta.id)
            # Alice: one unread inbox delivery + one already-read.
            run1 = await _run(ta.id, alice.id, a_assistant.id)
            alice_delivery = await deliveries_a.create(
                recipient_id=alice.id, run_id=run1.id, schedule_id=None,
                kind=RunDeliveryKind.INBOX, status=RunDeliveryStatus.DELIVERED,
                summary="Alice run ready.",
            )
            run2 = await _run(ta.id, alice.id, a_assistant.id)
            alice_read = await deliveries_a.create(
                recipient_id=alice.id, run_id=run2.id, schedule_id=None,
                kind=RunDeliveryKind.INBOX, status=RunDeliveryStatus.READ,
                summary="Old run.",
            )
            # Bob: a delivery in the same tenant, addressed to a different user.
            run3 = await _run(ta.id, bob.id, a_assistant.id)
            bob_delivery = await deliveries_a.create(
                recipient_id=bob.id, run_id=run3.id, schedule_id=None,
                kind=RunDeliveryKind.INBOX, status=RunDeliveryStatus.DELIVERED,
                summary="Bob run.",
            )
            # Carol: a delivery in another tenant.
            run4 = await _run(tb.id, carol.id, b_assistant.id)
            carol_delivery = await RunDeliveryRepository(seed, tb.id).create(
                recipient_id=carol.id, run_id=run4.id, schedule_id=None,
                kind=RunDeliveryKind.INBOX, status=RunDeliveryStatus.DELIVERED,
                summary="Carol run.",
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                alice_delivery=alice_delivery.id,
                alice_delivery_read=alice_read.id,
                bob_delivery=bob_delivery.id,
                carol_delivery=carol_delivery.id,
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
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session

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


# --- GET /run-deliveries (the inbox) ----------------------------------------


async def test_list_deliveries_returns_only_callers(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/run-deliveries", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {item["id"] for item in body["items"]}
    # Alice sees her two deliveries; not bob's (same tenant, other owner) nor carol's.
    assert str(seeded.alice_delivery) in ids
    assert str(seeded.alice_delivery_read) in ids
    assert str(seeded.bob_delivery) not in ids
    assert str(seeded.carol_delivery) not in ids
    item = next(i for i in body["items"] if i["id"] == str(seeded.alice_delivery))
    assert item["status"] == "delivered"
    assert item["summary"] == "Alice run ready."
    assert item["kind"] == "inbox"


async def test_list_deliveries_unread_only(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/run-deliveries?unread=true", headers=_auth(token))
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    # Only the unread delivery; the already-read one is excluded.
    assert ids == {str(seeded.alice_delivery)}


async def test_list_deliveries_filter_by_status(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/run-deliveries?status=read", headers=_auth(token))
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {str(seeded.alice_delivery_read)}


# --- POST /run-deliveries/{id}/read -----------------------------------------


async def test_mark_read_transitions_and_stamps(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/run-deliveries/{seeded.alice_delivery}/read", headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(seeded.alice_delivery)
    assert body["status"] == "read"
    assert body["read_at"] is not None
    # It now drops out of the unread inbox.
    unread = await client.get("/api/v1/run-deliveries?unread=true", headers=_auth(token))
    assert str(seeded.alice_delivery) not in {i["id"] for i in unread.json()["items"]}


# --- Negatives: INV-1 / INV-2 / INV-4 ---------------------------------------


async def test_cross_tenant_mark_read_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-1: a delivery in another tenant is 404 (existence non-disclosure)."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/run-deliveries/{seeded.carol_delivery}/read", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_other_owner_mark_read_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-2: a delivery addressed to another user in the same tenant is 404, not 403."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/run-deliveries/{seeded.bob_delivery}/read", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_unknown_delivery_id_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/run-deliveries/{uuid.uuid4()}/read", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_deliveries_require_a_token(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-4: no bearer token → 401 on both routes."""
    assert (await client.get("/api/v1/run-deliveries")).status_code == 401
    assert (
        await client.post(f"/api/v1/run-deliveries/{seeded.alice_delivery}/read")
    ).status_code == 401


async def test_deliveries_reject_a_bad_token(client: AsyncClient, seeded: _Seeded) -> None:
    resp = await client.get("/api/v1/run-deliveries", headers=_auth("not-a-real-token"))
    assert resp.status_code == 401
