"""Admin API tests — the read-mostly /admin/* contract + required negatives (#87).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite
database (no Postgres needed), mirroring ``test_collections_api``: the app's
``get_db_session`` dependency is overridden to yield sessions from a StaticPool
SQLite engine whose schema is built from the ORM metadata. Two tenants with a
mix of roles are seeded so the role-gating and tenant-scoping negatives are real.

The /admin/* surface is **read-only governance** (ADR-0007/§4 read-before-write):
* GET /admin/members — the tenant's members + roles (admin only), cursor-paged;
* GET /admin/model-governance — allowed models + governance tiers (admin only);
* GET /admin/risk-tiers — the T0–T3 read-before-write reference (admin only).

Negatives (the issue's named set + spec 0004 §3):
* INV-5: a non-admin (member / security) on **every** /admin path → **403**;
* INV-4: missing / malformed bearer → **401**;
* INV-1: /admin/members returns only the **caller's own tenant's** members.
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

_ADMIN_PATHS = (
    "/api/v1/admin/members",
    "/api/v1/admin/model-governance",
    "/api/v1/admin/risk-tiers",
    "/api/v1/admin/settings",
)


class _Seeded:
    """Identifiers for the seeded fixture graph (two tenants, several roles)."""

    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        admin_a_email: str,
        member_a_email: str,
        security_a_email: str,
        admin_b_email: str,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.admin_a_email = admin_a_email
        self.member_a_email = member_a_email
        self.security_a_email = security_a_email
        self.admin_b_email = admin_b_email


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A StaticPool SQLite engine + schema; seed two tenants with mixed roles."""
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
                email="member@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await UserRepository(seed, tenant_a.id).create(
                email="security@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.SECURITY],
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
            admin_a_email="admin@acme.test",
            member_a_email="member@acme.test",
            security_a_email="security@acme.test",
            admin_b_email="admin@globex.test",
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


async def _seed_member(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    email: str,
    roles: list[Role],
) -> uuid.UUID:
    """Insert an extra user directly (for pagination tests); return its id."""
    async with sessionmaker() as session:
        user = await UserRepository(session, tenant_id).create(
            email=email, password_hash=hash_password(_PASSWORD), roles=roles
        )
        await session.commit()
        return user.id


# --- GET /admin/members (happy path) ---------------------------------------


async def test_members_lists_tenant_roster_with_roles(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/members", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) <= {"items", "next_cursor"}
    assert "items" in body

    by_email = {m["email"]: m for m in body["items"]}
    # Exactly the three tenant-A users — never tenant B's admin (INV-1).
    assert set(by_email) == {"admin@acme.test", "member@acme.test", "security@acme.test"}
    for member in body["items"]:
        assert set(member) == {"id", "email", "role"}
        assert isinstance(member["id"], str) and member["id"]
        assert isinstance(member["role"], list)
        assert all(r in {"member", "admin", "security"} for r in member["role"])
    assert by_email["admin@acme.test"]["role"] == ["admin"]
    assert by_email["security@acme.test"]["role"] == ["security"]


async def test_members_excludes_other_tenants(client: AsyncClient, seeded: _Seeded) -> None:
    # The other tenant's admin must never appear in tenant A's roster (INV-1).
    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/members", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    emails = {m["email"] for m in resp.json()["items"]}
    assert "admin@globex.test" not in emails

    # And tenant B's admin sees only tenant B (a single member).
    token_b = await _login(client, seeded.admin_b_email)
    resp_b = await client.get("/api/v1/admin/members", headers=_auth(token_b))
    assert resp_b.status_code == 200, resp_b.text
    emails_b = {m["email"] for m in resp_b.json()["items"]}
    assert emails_b == {"admin@globex.test"}


async def test_members_cursor_pagination_walks_all_pages(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Seed enough extra members that a small page size forces several pages.
    for i in range(5):
        await _seed_member(
            sessionmaker,
            tenant_id=seeded.tenant_a,
            email=f"extra{i}@acme.test",
            roles=[Role.MEMBER],
        )
    token = await _login(client, seeded.admin_a_email)

    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/admin/members", headers=_auth(token), params=params)
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
    # 3 seeded tenant-A users + 5 extras = 8 distinct ids.
    assert len(seen) == 8


async def test_members_malformed_cursor_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.get(
        "/api/v1/admin/members", headers=_auth(token), params={"cursor": "not-a-cursor"}
    )
    assert resp.status_code == 422


# --- GET /admin/model-governance (happy path) ------------------------------


async def test_model_governance_lists_allowed_models_and_tiers(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/model-governance", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"allowed_models", "tiers"}

    assert body["allowed_models"], "the curated registry must not be empty"
    for entry in body["allowed_models"]:
        assert {"model_id", "tier"} <= set(entry)
        assert set(entry) <= {"model_id", "tier", "label"}
        assert isinstance(entry["model_id"], str) and entry["model_id"]
        assert isinstance(entry["tier"], str) and entry["tier"]

    # The default registry seed surfaces (config-driven, #47).
    model_ids = {e["model_id"] for e in body["allowed_models"]}
    assert "openrouter/anthropic/claude-opus-4.8" in model_ids

    # Every tier an allowed model maps to is described in ``tiers``.
    referenced = {e["tier"] for e in body["allowed_models"]}
    described = {t["id"] for t in body["tiers"]}
    assert referenced <= described
    for tier in body["tiers"]:
        assert set(tier) == {"id", "description"}
        assert tier["description"]


# --- GET /admin/risk-tiers (happy path) ------------------------------------


async def test_risk_tiers_returns_T0_through_T3(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/risk-tiers", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items"}

    tiers = body["items"]
    assert [t["tier"] for t in tiers] == ["T0", "T1", "T2", "T3"]
    for tier in tiers:
        assert set(tier) == {"tier", "description", "approval"}
        assert tier["description"]
        assert tier["approval"]
    # T0 is the read-only MVP baseline (no approval); T2/T3 require human approval.
    by_tier = {t["tier"]: t for t in tiers}
    assert "none" in by_tier["T0"]["approval"].lower()
    assert "approval" in by_tier["T2"]["approval"].lower()


# --- Negative: role gating (INV-5 → 403 on EVERY /admin path) --------------


@pytest.mark.parametrize("path", _ADMIN_PATHS)
async def test_member_is_forbidden_on_every_admin_path(
    client: AsyncClient, seeded: _Seeded, path: str
) -> None:
    token = await _login(client, seeded.member_a_email)
    resp = await client.get(path, headers=_auth(token))
    assert resp.status_code == 403, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("path", _ADMIN_PATHS)
async def test_security_role_is_forbidden_on_every_admin_path(
    client: AsyncClient, seeded: _Seeded, path: str
) -> None:
    # ``security`` may read /audit, but admin governance is admin-only (INV-5).
    token = await _login(client, seeded.security_a_email)
    resp = await client.get(path, headers=_auth(token))
    assert resp.status_code == 403, resp.text


# --- Negative: authentication (INV-4 → 401 on every /admin path) -----------


@pytest.mark.parametrize("path", _ADMIN_PATHS)
async def test_no_token_is_401_on_every_admin_path(client: AsyncClient, path: str) -> None:
    resp = await client.get(path)
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("path", _ADMIN_PATHS)
async def test_malformed_token_is_401_on_every_admin_path(client: AsyncClient, path: str) -> None:
    resp = await client.get(path, headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


# --- GET/PATCH /admin/settings (per-tenant tool-turn budget, issue #148) -----


async def test_get_tenant_settings_returns_system_default(
    client: AsyncClient, seeded: _Seeded
) -> None:
    from app.core.config import get_settings

    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/settings", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"max_tool_turns", "max_tool_turns_is_default"}
    # A fresh tenant has no override → the configured system default, flagged.
    assert body["max_tool_turns"] == get_settings().chat_max_tool_turns
    assert body["max_tool_turns_is_default"] is True


async def test_patch_tenant_settings_sets_and_reads_back(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/settings", headers=_auth(token), json={"max_tool_turns": 12}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"max_tool_turns": 12, "max_tool_turns_is_default": False}
    # The override persists and reads back via GET (round-trip).
    got = await client.get("/api/v1/admin/settings", headers=_auth(token))
    assert got.json() == {"max_tool_turns": 12, "max_tool_turns_is_default": False}


async def test_patch_tenant_settings_null_resets_to_default(
    client: AsyncClient, seeded: _Seeded
) -> None:
    from app.core.config import get_settings

    token = await _login(client, seeded.admin_a_email)
    await client.patch("/api/v1/admin/settings", headers=_auth(token), json={"max_tool_turns": 7})
    # Clearing with null reverts the tenant to the system default.
    resp = await client.patch(
        "/api/v1/admin/settings", headers=_auth(token), json={"max_tool_turns": None}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "max_tool_turns": get_settings().chat_max_tool_turns,
        "max_tool_turns_is_default": True,
    }


@pytest.mark.parametrize("bad", [0, -1, 51, 1000])
async def test_patch_tenant_settings_out_of_range_is_422(
    client: AsyncClient, seeded: _Seeded, bad: int
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/settings", headers=_auth(token), json={"max_tool_turns": bad}
    )
    assert resp.status_code == 422, resp.text


async def test_patch_tenant_settings_missing_field_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch("/api/v1/admin/settings", headers=_auth(token), json={})
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("email_attr", ["member_a_email", "security_a_email"])
async def test_patch_tenant_settings_forbidden_for_non_admin(
    client: AsyncClient, seeded: _Seeded, email_attr: str
) -> None:
    # The write is admin-only (INV-5) — a member or security role is 403.
    token = await _login(client, getattr(seeded, email_attr))
    resp = await client.patch(
        "/api/v1/admin/settings", headers=_auth(token), json={"max_tool_turns": 10}
    )
    assert resp.status_code == 403, resp.text


async def test_patch_tenant_settings_unauthenticated_is_401(client: AsyncClient) -> None:
    resp = await client.patch("/api/v1/admin/settings", json={"max_tool_turns": 10})
    assert resp.status_code == 401


async def test_patch_tenant_settings_is_tenant_scoped(client: AsyncClient, seeded: _Seeded) -> None:
    # Admin A sets an override; admin B's tenant is untouched (INV-1).
    token_a = await _login(client, seeded.admin_a_email)
    await client.patch("/api/v1/admin/settings", headers=_auth(token_a), json={"max_tool_turns": 9})
    token_b = await _login(client, seeded.admin_b_email)
    got_b = await client.get("/api/v1/admin/settings", headers=_auth(token_b))
    assert got_b.json()["max_tool_turns_is_default"] is True


async def test_patch_tenant_settings_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.db.repositories import AuditEventRepository

    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/settings", headers=_auth(token), json={"max_tool_turns": 15}
    )
    assert resp.status_code == 200, resp.text
    # The write emits exactly one audit event for this tenant (INV-6).
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    settings_events = [e for e in events if e.action == "tenant.settings_updated"]
    assert len(settings_events) == 1
    ev = settings_events[0]
    assert ev.metadata["max_tool_turns"] == 15
    assert ev.resource_type == "tenant"
    assert ev.resource_id == str(seeded.tenant_a)
