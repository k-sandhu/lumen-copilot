"""Tool-catalogue API tests — ``GET /tools`` for the assistant builder (issue #505).

The assistant builder needs to know **which tools exist, how risky each one is, and
whether the tenant's admin has switched it off** so it can offer a truthful
allow-list picker instead of a hardcoded stub. ``/admin/tool-policy`` already
exposes exactly that data, but it is ``admin``-only (INV-5) — an ordinary assistant
owner gets a 403 there, which is why ``run_python`` was ungrantable through the UI
and had to be seeded straight into the database.

``GET /tools`` is the **read-only, member-safe** projection of the same per-tenant
policy: every registered tool with its static registry metadata (``description`` /
``risk_tier`` / ``read_only``) plus the effective per-tenant governance flags
(``enabled`` / ``requires_approval`` / ``is_default``). It grants nothing and writes
nothing — the tool policy gate, the autonomy seam and the assistant allow-list
validation are all unchanged; this endpoint only lets the UI *tell the truth* about
them.

Covered here, end-to-end against the real FastAPI app over offline SQLite:

* a **non-admin member** can read the catalogue (the builder's caller), and
  ``run_python`` is present as T2 / not read-only / ``requires_approval`` by default;
* a read-only retrieval tool is T0 and never gated;
* an admin's **disable** override is reflected (``enabled=false``, ``is_default``
  false) so the builder can show it as unavailable rather than offering a control
  that silently fails;
* tenant isolation (INV-1): tenant A's override never colours tenant B's catalogue;
* no bearer token → **401** (INV-4).
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
from app.db.repositories import TenantRepository, TenantToolPolicyRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        admin_a_email: str,
        member_a_email: str,
        member_b_email: str,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.admin_a_email = admin_a_email
        self.member_a_email = member_a_email
        self.member_b_email = member_b_email


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
            await UserRepository(seed, tenant_b.id).create(
                email="member@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await seed.commit()
        factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
            tenant_a=tenant_a.id,
            tenant_b=tenant_b.id,
            admin_a_email="admin@acme.test",
            member_a_email="member@acme.test",
            member_b_email="member@globex.test",
        )
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


def _entry(body: dict[str, object], tool_name: str) -> dict[str, object]:
    items = body["items"]
    assert isinstance(items, list)
    matches = [e for e in items if e["tool_name"] == tool_name]
    assert matches, f"{tool_name} not in catalogue: {[e['tool_name'] for e in items]}"
    return matches[0]  # type: ignore[no-any-return]


async def _set_policy(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    tool_name: str,
    enabled: bool,
    requires_approval: bool,
) -> None:
    async with sessionmaker() as session:
        await TenantToolPolicyRepository(session, tenant_id).upsert(
            tool_name=tool_name,
            enabled=enabled,
            requires_approval=requires_approval,
            updated_by=None,
        )
        await session.commit()


# --- The builder's read: every registered tool, with governance ---------------


async def test_member_reads_catalogue_with_run_python_and_its_risk_tier(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """A NON-admin (the assistant owner) can read the catalogue — the #505 unlock.

    Without this the builder had no member-readable source of tool names, so
    ``run_python`` could only be granted by a direct database write.
    """
    from app.services.tools.registry import registered_names

    token = await _login(client, seeded.member_a_email)
    resp = await client.get("/api/v1/tools", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    names = [e["tool_name"] for e in body["items"]]
    assert set(names) == registered_names()
    assert names == sorted(names)

    run_python = _entry(body, "run_python")
    # The T2, side-effecting affordance the UI must render (spec 0004 §2.5).
    assert run_python["risk_tier"] == "T2"
    assert run_python["read_only"] is False
    # Deny-by-default until an admin pre-approves it (no override seeded here).
    assert run_python["requires_approval"] is True
    assert run_python["enabled"] is True
    assert run_python["is_default"] is True
    # A human-readable summary for the checkbox, straight from the registry.
    assert isinstance(run_python["description"], str) and run_python["description"]

    search = _entry(body, "search_text")
    assert search["risk_tier"] == "T0"
    assert search["read_only"] is True
    assert search["requires_approval"] is False


async def test_catalogue_reflects_an_admin_disable_override(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A tenant-disabled tool reads back ``enabled=false`` — the UI shows it unavailable."""
    await _set_policy(
        sessionmaker,
        tenant_id=seeded.tenant_a,
        tool_name="run_python",
        enabled=False,
        requires_approval=True,
    )
    token = await _login(client, seeded.member_a_email)
    resp = await client.get("/api/v1/tools", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    run_python = _entry(resp.json(), "run_python")
    assert run_python["enabled"] is False
    assert run_python["is_default"] is False


async def test_catalogue_is_tenant_scoped(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Tenant A's override never colours tenant B's catalogue (INV-1)."""
    await _set_policy(
        sessionmaker,
        tenant_id=seeded.tenant_a,
        tool_name="run_python",
        enabled=False,
        requires_approval=True,
    )
    token = await _login(client, seeded.member_b_email)
    resp = await client.get("/api/v1/tools", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    run_python = _entry(resp.json(), "run_python")
    assert run_python["enabled"] is True
    assert run_python["is_default"] is True


async def test_catalogue_body_validates_against_the_openapi_contract(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """The REAL response body validates against ``contracts/openapi.yaml``.

    contracts/AGENTS.md rule 5 ("no drift"): the emitted shape must match the frozen
    contract, so a rename on either side fails a test rather than a browser.
    """
    from pathlib import Path

    import jsonschema
    import yaml

    root = Path(__file__).resolve().parent.parent.parent
    spec = yaml.safe_load((root / "contracts" / "openapi.yaml").read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]

    token = await _login(client, seeded.member_a_email)
    resp = await client.get("/api/v1/tools", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    jsonschema.validate(resp.json(), {**schemas["ToolCatalog"], "components": {"schemas": schemas}})


# --- Negatives ---------------------------------------------------------------


async def test_catalogue_requires_a_bearer_token(client: AsyncClient) -> None:
    """No token → 401 (INV-4); the registry is not public."""
    resp = await client.get("/api/v1/tools")
    assert resp.status_code == 401


async def test_catalogue_is_read_only(client: AsyncClient, seeded: _Seeded) -> None:
    """The catalogue GRANTS nothing — there is no write verb on it (governance stays
    on ``/admin/tool-policy``, admin-only)."""
    token = await _login(client, seeded.member_a_email)
    resp = await client.patch(
        "/api/v1/tools",
        headers=_auth(token),
        json={"tool_name": "run_python", "enabled": True, "requires_approval": False},
    )
    assert resp.status_code == 405
