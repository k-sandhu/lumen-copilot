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
    "/api/v1/admin/tool-policy",
    "/api/v1/admin/sandbox-policy",
    "/api/v1/admin/autonomy-policy",
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


# --- GET/PATCH /admin/tool-policy (per-tenant tool governance, issue #223) ----


def _entry(body: dict[str, object], tool_name: str) -> dict[str, object]:
    """The tool-policy entry for ``tool_name`` from a GET/PATCH response body."""
    items = body["items"]
    assert isinstance(items, list)
    matches = [e for e in items if e["tool_name"] == tool_name]
    assert matches, f"{tool_name} not in policy: {[e['tool_name'] for e in items]}"
    return matches[0]  # type: ignore[no-any-return]


async def test_get_tool_policy_lists_every_registered_tool_with_defaults(
    client: AsyncClient, seeded: _Seeded
) -> None:
    from app.services.tools.registry import registered_names

    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/tool-policy", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items"}

    names = {e["tool_name"] for e in body["items"]}
    # Every registered tool appears (the registry is the source of truth).
    assert names == set(registered_names())
    for entry in body["items"]:
        assert set(entry) == {
            "tool_name",
            "risk_tier",
            "read_only",
            "enabled",
            "requires_approval",
            "is_default",
        }
        # A fresh tenant has NO overrides → every entry is the built-in default.
        assert entry["is_default"] is True

    # run_python (T2, requires_approval) is DENY-BY-DEFAULT: enabled but still
    # requiring approval (no admin pre-approval yet) — the gate denies it.
    run_python = _entry(body, "run_python")
    assert run_python["risk_tier"] == "T2"
    assert run_python["read_only"] is False
    assert run_python["requires_approval"] is True

    # A read-only retrieval tool is T0 and never gated.
    search = _entry(body, "search_text")
    assert search["risk_tier"] == "T0"
    assert search["read_only"] is True
    assert search["requires_approval"] is False


async def test_patch_tool_policy_enables_and_preapproves_run_python(
    client: AsyncClient, seeded: _Seeded
) -> None:
    # The run_python unlock: an admin enables AND pre-approves it for the tenant.
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/tool-policy",
        headers=_auth(token),
        json={"tool_name": "run_python", "enabled": True, "requires_approval": False},
    )
    assert resp.status_code == 200, resp.text
    run_python = _entry(resp.json(), "run_python")
    assert run_python["enabled"] is True
    assert run_python["requires_approval"] is False
    assert run_python["is_default"] is False  # an explicit override now exists

    # The override reads back via GET (round-trip).
    got = await client.get("/api/v1/admin/tool-policy", headers=_auth(token))
    assert _entry(got.json(), "run_python")["requires_approval"] is False


async def test_patch_tool_policy_unknown_tool_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/tool-policy",
        headers=_auth(token),
        json={"tool_name": "not_a_real_tool", "enabled": True, "requires_approval": False},
    )
    assert resp.status_code == 422, resp.text


async def test_patch_tool_policy_missing_field_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/tool-policy",
        headers=_auth(token),
        json={"tool_name": "run_python"},  # missing enabled/requires_approval
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("email_attr", ["member_a_email", "security_a_email"])
async def test_patch_tool_policy_forbidden_for_non_admin(
    client: AsyncClient, seeded: _Seeded, email_attr: str
) -> None:
    # The governance write is admin-only (INV-5) — member/security are 403.
    token = await _login(client, getattr(seeded, email_attr))
    resp = await client.patch(
        "/api/v1/admin/tool-policy",
        headers=_auth(token),
        json={"tool_name": "run_python", "enabled": True, "requires_approval": False},
    )
    assert resp.status_code == 403, resp.text


async def test_patch_tool_policy_unauthenticated_is_401(client: AsyncClient) -> None:
    resp = await client.patch(
        "/api/v1/admin/tool-policy",
        json={"tool_name": "run_python", "enabled": True, "requires_approval": False},
    )
    assert resp.status_code == 401


async def test_tool_policy_is_tenant_scoped(client: AsyncClient, seeded: _Seeded) -> None:
    # Admin A pre-approves run_python; admin B's tenant is untouched (INV-1).
    token_a = await _login(client, seeded.admin_a_email)
    await client.patch(
        "/api/v1/admin/tool-policy",
        headers=_auth(token_a),
        json={"tool_name": "run_python", "enabled": True, "requires_approval": False},
    )
    token_b = await _login(client, seeded.admin_b_email)
    got_b = await client.get("/api/v1/admin/tool-policy", headers=_auth(token_b))
    run_python_b = _entry(got_b.json(), "run_python")
    # Tenant B still sees the built-in default (deny-by-default), never A's override.
    assert run_python_b["is_default"] is True
    assert run_python_b["requires_approval"] is True


async def test_patch_tool_policy_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.db.repositories import AuditEventRepository

    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/tool-policy",
        headers=_auth(token),
        json={"tool_name": "run_python", "enabled": True, "requires_approval": False},
    )
    assert resp.status_code == 200, resp.text
    # The write emits exactly one audit event for this tenant (INV-6).
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    policy_events = [e for e in events if e.action == "tool_policy.updated"]
    assert len(policy_events) == 1
    ev = policy_events[0]
    assert ev.metadata["tool_name"] == "run_python"
    assert ev.metadata["enabled"] is True
    assert ev.metadata["requires_approval"] is False
    assert ev.resource_type == "tool"
    assert ev.resource_id == "run_python"


# --- GET/PATCH /admin/sandbox-policy (per-tenant sandbox governance, #233) -----

_SANDBOX_KEYS = {
    "enabled",
    "allowed_packages",
    "denied_packages",
    "egress_allowed",
    "egress_allowlist",
    "max_runtime_s",
    "max_memory_mb",
    "daily_runtime_cap_s",
    "max_concurrency",
    "is_default",
    "max_runtime_s_ceiling",
    "max_memory_mb_ceiling",
    "daily_runtime_cap_s_ceiling",
    "max_concurrency_ceiling",
}


def _sandbox_body(**overrides: object) -> dict[str, object]:
    """A complete, valid SandboxPolicyUpdate body; override any field."""
    body: dict[str, object] = {
        "enabled": True,
        "allowed_packages": [],
        "denied_packages": [],
        "egress_allowed": False,
        "egress_allowlist": [],
        "max_runtime_s": 20,
        "max_memory_mb": 256,
        "daily_runtime_cap_s": 600,
        "max_concurrency": 1,
    }
    body.update(overrides)
    return body


async def test_get_sandbox_policy_default_is_disabled_deny_by_default(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC-3 (#233): a fresh tenant has NO policy → code exec off, caps = config ceiling."""
    from app.core.config import get_settings

    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/sandbox-policy", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _SANDBOX_KEYS
    # Deny-by-default: disabled, no packages/egress, is_default true.
    assert body["enabled"] is False
    assert body["is_default"] is True
    assert body["egress_allowed"] is False
    assert body["allowed_packages"] == []
    # The reported caps ARE the deploy-wide config ceiling.
    settings = get_settings()
    assert body["max_runtime_s"] == settings.sandbox_wall_clock_seconds
    assert body["max_runtime_s_ceiling"] == settings.sandbox_wall_clock_seconds
    assert body["max_concurrency_ceiling"] == settings.sandbox_max_concurrent_per_tenant


async def test_patch_sandbox_policy_sets_and_reads_back(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC-1 (#233): an admin enables code exec + sets limits; the policy reads back."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy",
        headers=_auth(token),
        json=_sandbox_body(
            enabled=True, allowed_packages=["numpy"], max_runtime_s=20, max_concurrency=1
        ),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["is_default"] is False
    assert body["allowed_packages"] == ["numpy"]
    assert body["max_runtime_s"] == 20
    # The policy persists and reads back via GET (round-trip).
    got = await client.get("/api/v1/admin/sandbox-policy", headers=_auth(token))
    assert got.json()["enabled"] is True
    assert got.json()["allowed_packages"] == ["numpy"]


async def test_patch_sandbox_policy_clamps_runtime_to_the_config_ceiling(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC-2 (#233): a per-tenant cap ABOVE the config ceiling is clamped down — never widened."""
    from app.core.config import get_settings

    token = await _login(client, seeded.admin_a_email)
    ceiling = get_settings().sandbox_wall_clock_seconds
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy",
        headers=_auth(token),
        json=_sandbox_body(max_runtime_s=ceiling + 10_000, max_memory_mb=99_999),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The stored/effective runtime is the ceiling, NOT the widened value.
    assert body["max_runtime_s"] == ceiling
    assert body["max_runtime_s"] <= body["max_runtime_s_ceiling"]
    assert body["max_memory_mb"] <= body["max_memory_mb_ceiling"]


async def test_patch_sandbox_policy_strips_metadata_ip_from_egress(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC-2 (#233, G4): the cloud-metadata IP is stripped from the egress allowlist."""
    from app.sandbox.spec import METADATA_IP

    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy",
        headers=_auth(token),
        json=_sandbox_body(
            egress_allowed=True,
            egress_allowlist=[f"{METADATA_IP}:80", "api.example.com:443"],
        ),
    )
    assert resp.status_code == 200, resp.text
    allowlist = resp.json()["egress_allowlist"]
    # The metadata IP is never allowlistable — only the legitimate target remains.
    assert all(METADATA_IP not in t for t in allowlist)
    assert "api.example.com:443" in allowlist


@pytest.mark.parametrize(
    "field", ["max_runtime_s", "max_memory_mb", "daily_runtime_cap_s", "max_concurrency"]
)
async def test_patch_sandbox_policy_non_positive_cap_is_422(
    client: AsyncClient, seeded: _Seeded, field: str
) -> None:
    """AC (#233, INV-8): a non-positive cap is rejected 422 — no unbounded value stored."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy",
        headers=_auth(token),
        json=_sandbox_body(**{field: 0}),
    )
    assert resp.status_code == 422, resp.text


async def test_patch_sandbox_policy_unknown_field_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC (#233, INV-8): an unknown/extra field is rejected 422 (extra=forbid)."""
    token = await _login(client, seeded.admin_a_email)
    body = _sandbox_body()
    body["not_a_real_field"] = True
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy", headers=_auth(token), json=body
    )
    assert resp.status_code == 422, resp.text


async def test_patch_sandbox_policy_missing_field_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC (#233, INV-8): a missing required field is rejected 422."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy",
        headers=_auth(token),
        json={"enabled": True},  # missing everything else
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("email_attr", ["member_a_email", "security_a_email"])
async def test_patch_sandbox_policy_forbidden_for_non_admin(
    client: AsyncClient, seeded: _Seeded, email_attr: str
) -> None:
    """AC-1 (#233, INV-5): the governance write is admin-only — member/security are 403."""
    token = await _login(client, getattr(seeded, email_attr))
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy", headers=_auth(token), json=_sandbox_body()
    )
    assert resp.status_code == 403, resp.text


async def test_patch_sandbox_policy_unauthenticated_is_401(client: AsyncClient) -> None:
    resp = await client.patch("/api/v1/admin/sandbox-policy", json=_sandbox_body())
    assert resp.status_code == 401


async def test_sandbox_policy_is_tenant_scoped(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-1 (#233): admin A enables the sandbox; admin B's tenant stays deny-by-default."""
    token_a = await _login(client, seeded.admin_a_email)
    await client.patch(
        "/api/v1/admin/sandbox-policy",
        headers=_auth(token_a),
        json=_sandbox_body(enabled=True),
    )
    token_b = await _login(client, seeded.admin_b_email)
    got_b = await client.get("/api/v1/admin/sandbox-policy", headers=_auth(token_b))
    # Tenant B still sees the deny-by-default default, never A's enabled policy.
    assert got_b.json()["enabled"] is False
    assert got_b.json()["is_default"] is True


async def test_patch_sandbox_policy_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """AC-N (#233, INV-6): the write emits exactly one audit event for this tenant."""
    from app.db.repositories import AuditEventRepository

    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/sandbox-policy",
        headers=_auth(token),
        json=_sandbox_body(enabled=True, egress_allowed=True, egress_allowlist=["h:1"]),
    )
    assert resp.status_code == 200, resp.text
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    policy_events = [e for e in events if e.action == "sandbox_policy.updated"]
    assert len(policy_events) == 1
    ev = policy_events[0]
    assert ev.metadata["enabled"] is True
    assert ev.metadata["egress_allowed"] is True
    assert ev.resource_type == "tenant"
    assert ev.resource_id == str(seeded.tenant_a)


# --- GET/PATCH /admin/autonomy-policy (per-tenant autonomy cap, #218) ----------

_AUTONOMY_KEYS = {"max_autonomy", "is_default", "levels"}


async def test_get_autonomy_policy_default_is_no_ceiling(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC-3 (#218): a fresh tenant has NO cap → is_default true, ceiling = act_auto."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.get("/api/v1/admin/autonomy-policy", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == _AUTONOMY_KEYS
    # No stored cap ⇒ no ceiling: reported as the top of the scale, is_default true.
    assert body["is_default"] is True
    assert body["max_autonomy"] == "act_auto"
    assert body["levels"] == ["suggest", "draft", "act_with_approval", "act_auto"]


async def test_patch_autonomy_policy_sets_and_reads_back(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC-2 (#218): an admin sets the cap; it reads back and is no longer default."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/autonomy-policy",
        headers=_auth(token),
        json={"max_autonomy": "draft"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["max_autonomy"] == "draft"
    assert body["is_default"] is False
    # The cap persists and reads back via GET (round-trip).
    got = await client.get("/api/v1/admin/autonomy-policy", headers=_auth(token))
    assert got.json()["max_autonomy"] == "draft"
    assert got.json()["is_default"] is False


async def test_patch_autonomy_policy_unknown_level_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC (#218, INV-8): an unknown autonomy value is rejected 422 — no free-text ceiling."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/autonomy-policy",
        headers=_auth(token),
        json={"max_autonomy": "godmode"},
    )
    assert resp.status_code == 422, resp.text


async def test_patch_autonomy_policy_unknown_field_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """AC (#218, INV-8): an extra field is rejected 422 (extra=forbid)."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/autonomy-policy",
        headers=_auth(token),
        json={"max_autonomy": "draft", "not_a_field": True},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("email_attr", ["member_a_email", "security_a_email"])
async def test_patch_autonomy_policy_forbidden_for_non_admin(
    client: AsyncClient, seeded: _Seeded, email_attr: str
) -> None:
    """AC-N (#218, INV-5): the governance write is admin-only — member/security are 403."""
    token = await _login(client, getattr(seeded, email_attr))
    resp = await client.patch(
        "/api/v1/admin/autonomy-policy",
        headers=_auth(token),
        json={"max_autonomy": "draft"},
    )
    assert resp.status_code == 403, resp.text


async def test_patch_autonomy_policy_unauthenticated_is_401(client: AsyncClient) -> None:
    resp = await client.patch(
        "/api/v1/admin/autonomy-policy", json={"max_autonomy": "draft"}
    )
    assert resp.status_code == 401


async def test_autonomy_policy_is_tenant_scoped(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """INV-1 (#218): admin A sets a cap; admin B's tenant stays at the no-ceiling default."""
    token_a = await _login(client, seeded.admin_a_email)
    await client.patch(
        "/api/v1/admin/autonomy-policy",
        headers=_auth(token_a),
        json={"max_autonomy": "suggest"},
    )
    token_b = await _login(client, seeded.admin_b_email)
    got_b = await client.get("/api/v1/admin/autonomy-policy", headers=_auth(token_b))
    # Tenant B still sees the no-ceiling default, never A's cap.
    assert got_b.json()["is_default"] is True
    assert got_b.json()["max_autonomy"] == "act_auto"


async def test_patch_autonomy_policy_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """AC-N (#218, INV-6): the write emits exactly one autonomy_cap.updated event."""
    from app.db.repositories import AuditEventRepository

    token = await _login(client, seeded.admin_a_email)
    resp = await client.patch(
        "/api/v1/admin/autonomy-policy",
        headers=_auth(token),
        json={"max_autonomy": "act_with_approval"},
    )
    assert resp.status_code == 200, resp.text
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    cap_events = [e for e in events if e.action == "autonomy_cap.updated"]
    assert len(cap_events) == 1
    ev = cap_events[0]
    assert ev.metadata["max_autonomy"] == "act_with_approval"
    assert ev.resource_type == "tenant"
    assert ev.resource_id == str(seeded.tenant_a)
