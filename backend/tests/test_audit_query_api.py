"""Audit API tests — the GET /audit contract + required negatives (#85).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite
database (no Postgres needed), mirroring ``test_collections_api``: the app's
``get_db_session`` dependency is overridden to yield sessions from a StaticPool
SQLite engine whose schema is created from the ORM metadata. Users with
different roles in two tenants are seeded so the role-gate and tenancy negatives
are real:

* happy path: an ``admin``/``security`` reader gets a page of their tenant's
  events with the contract shape (``items`` + ``next_cursor``; each event carries
  ``provenance``);
* filters: actor / event_type / resource_id narrow the page;
* negatives (spec 0004 §3):
  - **INV-5**: a ``member`` (any non-admin/security role) → **403**;
  - **INV-1**: cross-tenant events are excluded — a reader never sees another
    tenant's audit events;
  - **INV-4**: no / malformed bearer → 401.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session
from app.auth import hash_password
from app.db.base import Base
from app.db.repositories import AuditEventRepository, TenantRepository, UserRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Role
from app.main import create_app
from app.services.audit import AuditSink

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PASSWORD = "devpassword"


class _Seeded:
    """Identifiers for the seeded fixture graph (two tenants, four users)."""

    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        admin_email: str,
        security_email: str,
        member_email: str,
        b_admin_email: str,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.admin_email = admin_email  # tenant A admin (reader under test)
        self.security_email = security_email  # tenant A security (also a reader)
        self.member_email = member_email  # tenant A member (must be 403)
        self.b_admin_email = b_admin_email  # tenant B admin (cross-tenant)


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A StaticPool SQLite engine + schema; seed two tenants and four users."""
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
                email="admin@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.ADMIN],
            )
            await UserRepository(seed, tenant_a.id).create(
                email="security@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.SECURITY],
            )
            await UserRepository(seed, tenant_a.id).create(
                email="member@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await UserRepository(seed, tenant_b.id).create(
                email="admin@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.ADMIN],
            )
            await seed.commit()
        factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
            tenant_a=tenant_a.id,
            tenant_b=tenant_b.id,
            admin_email="admin@acme.test",
            security_email="security@acme.test",
            member_email="member@acme.test",
            b_admin_email="admin@globex.test",
        )
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


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


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_event(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    action: AuditAction | str = AuditAction.DOCUMENT_VIEWED,
    actor: AuditActor | None = None,
    resource_type: str = "document",
    resource_id: str | None = None,
    outcome: AuditOutcome = AuditOutcome.ALLOWED,
    metadata: dict[str, object] | None = None,
) -> uuid.UUID:
    """Emit one audit event through the real sink; return its id."""
    async with sessionmaker() as session:
        sink = AuditSink(AuditEventRepository(session, tenant_id))
        event = await sink.emit(
            action=action,
            actor=actor or AuditActor.system(),
            resource_type=resource_type,
            resource_id=resource_id or str(uuid.uuid4()),
            outcome=outcome,
            request_id="req-test",
            source_ip="203.0.113.7",
            metadata=metadata,
        )
        await session.commit()
        return event.id


# --- Happy path -------------------------------------------------------------


async def test_admin_lists_audit_events_with_contract_shape(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seeded_id = str(
        await _seed_event(
            sessionmaker,
            tenant_id=seeded.tenant_a,
            action=AuditAction.RETRIEVAL_QUERY,
            resource_type="retrieval",
            metadata={"document_ids": [str(uuid.uuid4())], "hit_count": 1},
        )
    )
    token = await _login(client, seeded.admin_email)
    resp = await client.get("/api/v1/audit", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) <= {"items", "next_cursor"}
    assert "items" in body
    # Login itself audits an auth.login event, so the trail is not empty; locate
    # the event we seeded explicitly.
    event = next(e for e in body["items"] if e["id"] == seeded_id)
    assert set(event) <= {
        "id",
        "ts",
        "actor",
        "tenant_id",
        "event_type",
        "resource_id",
        "decision",
        "provenance",
    }
    assert event["event_type"] == "retrieval.query"
    assert event["decision"] == "allowed"
    assert str(seeded.tenant_a) == event["tenant_id"]
    # Provenance is always present with a candidates list.
    assert "candidates" in event["provenance"]
    assert len(event["provenance"]["candidates"]) == 1
    assert event["provenance"]["candidates"][0]["disposition"] == "allow"


async def test_security_role_can_read_audit(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seeded_id = str(await _seed_event(sessionmaker, tenant_id=seeded.tenant_a))
    token = await _login(client, seeded.security_email)
    resp = await client.get("/api/v1/audit", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {e["id"] for e in resp.json()["items"]}
    assert seeded_id in ids


async def test_trail_with_no_seeded_events_returns_only_login_audit(
    client: AsyncClient, seeded: _Seeded
) -> None:
    # No explicitly-seeded events, but logging in audits one auth.login event —
    # the trail surfaces it (and only the caller's tenant's, INV-1).
    token = await _login(client, seeded.admin_email)
    resp = await client.get(
        "/api/v1/audit", headers=_auth(token), params={"event_type": "auth.login"}
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()["items"]
    assert len(events) == 1
    assert events[0]["event_type"] == "auth.login"
    assert events[0]["tenant_id"] == str(seeded.tenant_a)


# --- Filters ----------------------------------------------------------------


async def test_filter_by_event_type(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Use event types login does not itself emit, so the filter result is exact.
    viewed = str(
        await _seed_event(
            sessionmaker, tenant_id=seeded.tenant_a, action=AuditAction.DOCUMENT_VIEWED
        )
    )
    await _seed_event(
        sessionmaker, tenant_id=seeded.tenant_a, action=AuditAction.DOCUMENT_DOWNLOADED
    )
    token = await _login(client, seeded.admin_email)
    resp = await client.get(
        "/api/v1/audit", headers=_auth(token), params={"event_type": "document.viewed"}
    )
    assert resp.status_code == 200, resp.text
    events = resp.json()["items"]
    assert len(events) == 1
    assert events[0]["id"] == viewed
    assert events[0]["event_type"] == "document.viewed"


async def test_filter_by_resource_id(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    target = str(uuid.uuid4())
    await _seed_event(sessionmaker, tenant_id=seeded.tenant_a, resource_id=target)
    await _seed_event(sessionmaker, tenant_id=seeded.tenant_a, resource_id=str(uuid.uuid4()))
    token = await _login(client, seeded.admin_email)
    resp = await client.get("/api/v1/audit", headers=_auth(token), params={"resource_id": target})
    assert resp.status_code == 200, resp.text
    events = resp.json()["items"]
    assert len(events) == 1
    assert events[0]["resource_id"] == target


async def test_filter_by_actor_user_id(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    alice = uuid.uuid4()
    await _seed_event(sessionmaker, tenant_id=seeded.tenant_a, actor=AuditActor.user(alice))
    await _seed_event(sessionmaker, tenant_id=seeded.tenant_a, actor=AuditActor.user(uuid.uuid4()))
    token = await _login(client, seeded.admin_email)
    resp = await client.get("/api/v1/audit", headers=_auth(token), params={"actor": str(alice)})
    assert resp.status_code == 200, resp.text
    events = resp.json()["items"]
    assert len(events) == 1
    assert events[0]["actor"] == str(alice)


async def test_cursor_pagination_walks_all_pages(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Seed under document.viewed (login does not emit it) and filter on it, so
    # the walked set is exactly what we created — undisturbed by the login audit.
    created: set[str] = set()
    for _ in range(5):
        created.add(
            str(
                await _seed_event(
                    sessionmaker, tenant_id=seeded.tenant_a, action=AuditAction.DOCUMENT_VIEWED
                )
            )
        )
    token = await _login(client, seeded.admin_email)
    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 2, "event_type": "document.viewed"}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/audit", headers=_auth(token), params=params)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) <= 2
        for event in body["items"]:
            assert event["id"] not in seen  # no duplicates across pages
            seen.add(event["id"])
        pages += 1
        cursor = body.get("next_cursor")
        if cursor is None:
            break
        assert pages < 10  # guard against a non-terminating cursor
    assert seen == created


async def test_malformed_cursor_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.admin_email)
    resp = await client.get(
        "/api/v1/audit", headers=_auth(token), params={"cursor": "not-a-cursor!!"}
    )
    assert resp.status_code == 422


# --- Negative: role gate (INV-5 → 403, never the data) ----------------------


async def test_member_is_forbidden_403(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Even with events present, a member never reads the trail.
    await _seed_event(sessionmaker, tenant_id=seeded.tenant_a)
    token = await _login(client, seeded.member_email)
    resp = await client.get(
        "/api/v1/audit",
        headers={**_auth(token), "x-request-id": "req-audit-denied"},
    )
    assert resp.status_code == 403
    assert resp.headers["content-type"].startswith("application/problem+json")

    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
        member = await UserRepository(session, seeded.tenant_a).get_by_email(seeded.member_email)
    assert member is not None
    denied = [event for event in events if event.action == "permission.denied"]
    assert len(denied) == 1
    event = denied[0]
    assert event.tenant_id == seeded.tenant_a
    assert event.actor_id == member.id
    assert event.outcome is AuditOutcome.DENIED
    assert event.resource_type == "api_route"
    assert event.resource_id == "/api/v1/audit"
    assert event.request_id == "req-audit-denied"
    assert event.source_origin == "client"
    assert event.source_ip is not None
    assert event.metadata == {
        "attempted_action": "GET /api/v1/audit",
        "reason": "missing_required_role",
        "required_roles": ["admin", "security"],
    }


# --- Negative: tenant isolation (INV-1 → cross-tenant excluded) -------------


async def test_cross_tenant_events_excluded(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    mine = str(await _seed_event(sessionmaker, tenant_id=seeded.tenant_a))
    # Tenant B has its own events — they must never appear in tenant A's trail.
    b1 = str(await _seed_event(sessionmaker, tenant_id=seeded.tenant_b))
    b2 = str(await _seed_event(sessionmaker, tenant_id=seeded.tenant_b))
    token = await _login(client, seeded.admin_email)
    resp = await client.get("/api/v1/audit", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {e["id"] for e in resp.json()["items"]}
    # Every returned event belongs to tenant A; B's events are absent (INV-1).
    assert mine in ids
    assert b1 not in ids
    assert b2 not in ids
    assert all(e["tenant_id"] == str(seeded.tenant_a) for e in resp.json()["items"])


async def test_cross_tenant_resource_filter_does_not_leak(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A resource_id present in BOTH tenants: filtering must still be tenant-scoped.
    shared = str(uuid.uuid4())
    mine = str(await _seed_event(sessionmaker, tenant_id=seeded.tenant_a, resource_id=shared))
    await _seed_event(sessionmaker, tenant_id=seeded.tenant_b, resource_id=shared)
    token = await _login(client, seeded.admin_email)
    resp = await client.get("/api/v1/audit", headers=_auth(token), params={"resource_id": shared})
    assert resp.status_code == 200, resp.text
    ids = [e["id"] for e in resp.json()["items"]]
    assert ids == [mine]


async def test_b_admin_sees_only_b_events(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    a_event = str(await _seed_event(sessionmaker, tenant_id=seeded.tenant_a))
    b_event = str(await _seed_event(sessionmaker, tenant_id=seeded.tenant_b))
    token = await _login(client, seeded.b_admin_email)
    resp = await client.get("/api/v1/audit", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {e["id"] for e in resp.json()["items"]}
    # B's reader sees B's seeded event but never A's (INV-1).
    assert b_event in ids
    assert a_event not in ids
    assert all(e["tenant_id"] == str(seeded.tenant_b) for e in resp.json()["items"])


# --- Negative: authentication (INV-4 → 401) --------------------------------


async def test_without_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/audit")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_malformed_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/audit", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


# --- Discovery: the router is auto-registered (ADR-0008 §3) -----------------


def test_audit_router_is_auto_discovered() -> None:
    from app.api.v1 import discover_router_modules

    discovered: Sequence[str] = discover_router_modules()
    assert "app.api.v1.audit" in discovered


def test_contract_audit_enum_declares_the_group_actions() -> None:
    """``AuditEventType`` must admit the ``group.*`` actions (ADR-0022).

    ``GET /audit?type=`` is typed by the contract's **closed** enum, so an action
    the backend emits but the contract omits is unfilterable, and a generated
    client would reject the value outright. Nothing enforced this, which is how
    the ``group.*`` actions were first added to ``AuditAction`` without reaching
    the contract.

    Scoped deliberately to the group actions: the contract currently declares 19
    of the 84 values ``AuditAction`` can emit, and closing that pre-existing gap
    means deciding, per feature, which events are client-filterable — tracked
    separately rather than guessed at here.
    """
    import yaml

    from app.domain.audit import AuditAction

    contract = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"
    spec = yaml.safe_load(contract.read_text(encoding="utf-8"))
    declared = set(spec["components"]["schemas"]["AuditEventType"]["enum"])
    group_actions = {a.value for a in AuditAction if a.value.startswith("group.")}

    assert group_actions, "the group.* audit actions must exist on AuditAction"
    missing = sorted(group_actions - declared)
    assert missing == [], f"emitted but absent from the contract enum: {missing}"
