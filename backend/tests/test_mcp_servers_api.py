"""MCP servers API tests — the /mcp-servers contract + required negatives (#226).

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite DB
(no Postgres) with the MCP adapter pointed at an in-process FastMCP fixture server
(no socket) and the per-tenant rate limiter faked (no Redis). The mcp-servers
router is auto-discovered, so hitting ``/api/v1/mcp-servers`` exercises the real
router/service/repository + the real MCP SDK handshake + the real SSRF guard.

The FastMCP fixture's streamable-HTTP session manager is an anyio task group whose
cancel scope must be entered/exited in the **same task**, so — like the #225
adapter tests — the fixture is entered *inside each test body* (``async with
_offline(...)``) rather than as a pytest fixture (which enters setup and teardown
in different tasks and trips the anyio cancel-scope guard).

Covered (ACs + mandatory negatives, ADR-0012 §5):

* **AC-1**: register (201, pending) → test (discovers + persists tools + health,
  status=ready) → list → get → tools → delete (204);
* **AC-2 / secret-never-returned**: register with auth → get/list/test/update
  responses carry a masked ``secret_hint`` but NEVER the credential value;
* **AC-3 / SSRF**: a loopback / private / metadata / non-https endpoint → **422**
  (``endpoint_blocked`` / ``endpoint_scheme``, egress via the shared guard); an
  unsupported / ``stdio`` transport → **422** ``unsupported_transport``;
* **AC-4 / INV-1/INV-2**: another tenant's/user's server id → **404** (never 403);
  list is owner-scoped;
* **failure isolation** (§7): a down server's ``test`` → status ``error`` + a safe
  ``last_error``, HTTP **200**, never a crash;
* **INV-6**: register/test/delete are audited; **INV-4**: no/bad bearer → 401.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata
from app.api.deps import get_db_session
from app.auth import hash_password
from app.db.base import Base
from app.db.models import AuditEvent
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role
from app.main import create_app
from app.services import mcp_servers_service as svc
from app.services.mcp_servers_service import (
    McpClientParams,
    build_transport_client_factory,
)
from tests._mcp_fixture_server import fixture_mcp

_PASSWORD = "devpassword"

# The MCP fixture host + a PUBLIC IP its DNS is stubbed to (so the SSRF guard admits
# the connect and the in-process ASGI transport serves it). A test that wants a
# BLOCKED target uses a literal loopback/private/metadata URL instead.
_FIXTURE_HOST = "fixture-mcp.example.com"
_FIXTURE_URL = f"https://{_FIXTURE_HOST}/mcp"
_PUBLIC_IP = "93.184.216.34"

# A path the in-process fixture ASGI app does NOT serve (only /mcp is mounted), so
# the MCP handshake gets a 404 and fails cleanly — the §7 down-server case, driven
# through the SAME injected transport (which routes by ASGI app, not by host, so a
# genuinely-unreachable host cannot be simulated by DNS; a wrong path can).
_DOWN_HOST = "down-mcp.example.com"
_DOWN_URL = f"https://{_FIXTURE_HOST}/not-an-mcp-endpoint"

_BEARER_SECRET = "super-secret-token-value-1234"


class _Seeded:
    def __init__(self, *, tenant_a: uuid.UUID, tenant_b: uuid.UUID) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_email = "alice@acme.test"  # tenant A owner under test
        self.bob_email = "bob@acme.test"  # tenant A, other owner
        self.carol_email = "carol@globex.test"  # tenant B owner


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
        factory.seeded = _Seeded(tenant_a=tenant_a.id, tenant_b=tenant_b.id)  # type: ignore[attr-defined]
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture(autouse=True)
def _stub_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the fixture + down hosts to a PUBLIC IP so the SSRF guard admits them.

    The guard still runs in full (range check + IP-pin); we only make these hosts
    look public so a connect reaches the in-process ASGI server (the fixture) or
    fails to connect (the down host). A blocked-target test uses a literal
    private/loopback/metadata URL, which never reaches this stub.
    """
    real = socket.getaddrinfo

    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        if host in (_FIXTURE_HOST, _DOWN_HOST):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]
        return real(host, *args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


class _FakeLimiter:
    """Records the tenant it was asked about; always admits (no Redis)."""

    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []

    def try_acquire(self, tenant_id: uuid.UUID) -> bool:
        self.calls.append(tenant_id)
        return True


@asynccontextmanager
async def _offline(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AsyncClient, _FakeLimiter]]:
    """Yield an ASGI client wired to an offline MCP adapter + fake rate limiter.

    Enters the FastMCP fixture lifespan **in the test's own task** (anyio cancel
    scope discipline), patches the router's ``build_mcp_servers_service`` to inject a
    fixture-bound client factory (the SSRF guard still wraps the fixture's in-process
    transport — the real round-trip, no socket) and the fake limiter, and overrides
    the DB session to the in-memory SQLite. CC-C secrets (SQLite + the dev cipher),
    the repository, and the audit sink are all real.
    """
    async with fixture_mcp() as fx:
        limiter = _FakeLimiter()
        real_build = svc.build_mcp_servers_service
        factory = build_transport_client_factory(fx.inner_transport)

        def _patched(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            kwargs["client_factory"] = factory
            kwargs["rate_limiter"] = limiter
            return real_build(*args, **kwargs)

        monkeypatch.setattr("app.api.v1.mcp_servers.build_mcp_servers_service", _patched)

        application = create_app()

        async def _override_session() -> AsyncIterator[AsyncSession]:
            async with sessionmaker() as session:
                yield session

        application.dependency_overrides[get_db_session] = _override_session
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac, limiter
        application.dependency_overrides.clear()


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(
    client: AsyncClient,
    token: str,
    *,
    url: str = _FIXTURE_URL,
    transport: str = "streamable_http",
    auth: dict | None = None,
) -> object:
    body: dict[str, object] = {"name": "fixture", "transport": transport, "endpoint_url": url}
    if auth is not None:
        body["auth"] = auth
    return await client.post("/api/v1/mcp-servers", json=body, headers=_auth(token))


async def _audit_actions(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[str]:
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(AuditEvent.action).where(AuditEvent.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


# --- AC-1: happy path -------------------------------------------------------


async def test_register_test_list_get_tools_delete_round_trip(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, limiter):
        token = await _login(client, seeded.alice_email)

        # register → 201 pending
        resp = await _register(client, token)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        assert body["transport"] == "streamable_http"
        assert body["discovered_tool_count"] == 0
        server_id = body["id"]

        # test → 200; discovers the fixture's tools (echo + boom) + health ready
        resp = await client.post(f"/api/v1/mcp-servers/{server_id}/test", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        tested = resp.json()
        assert tested["status"] == "ready"
        assert tested["last_health_at"] is not None
        assert tested["discovered_tool_count"] == 2
        # The per-tenant MCP egress rate limit was consulted (bound to the limiter).
        assert seeded.tenant_a in limiter.calls

        # list → the one server (owner-scoped)
        resp = await client.get("/api/v1/mcp-servers", headers=_auth(token))
        assert resp.status_code == 200
        assert [s["id"] for s in resp.json()["items"]] == [server_id]

        # get → status persisted as ready
        resp = await client.get(f"/api/v1/mcp-servers/{server_id}", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

        # tools → the discovered tools, namespaced (mcp:<slug>:<raw>) + risk-tiered.
        resp = await client.get(f"/api/v1/mcp-servers/{server_id}/tools", headers=_auth(token))
        assert resp.status_code == 200
        tools = {t["raw_name"]: t for t in resp.json()["items"]}
        assert set(tools) == {"echo", "boom"}
        assert tools["echo"]["name"].startswith("mcp:")
        assert tools["echo"]["name"].endswith(":echo")
        assert tools["echo"]["input_schema"]["type"] == "object"
        # The FastMCP fixture advertises no read-only annotation, so both tools
        # default to T2 (trust is earned — unannotated ⇒ approval-gated, ADR-0012 §6).
        assert tools["echo"]["read_only"] is False
        assert tools["echo"]["risk_tier"] == "T2"
        assert tools["boom"]["risk_tier"] == "T2"

        # delete → 204, gone
        resp = await client.delete(f"/api/v1/mcp-servers/{server_id}", headers=_auth(token))
        assert resp.status_code == 204
        resp = await client.get("/api/v1/mcp-servers", headers=_auth(token))
        assert resp.json()["items"] == []

    # INV-6: the lifecycle is audited.
    actions = await _audit_actions(sessionmaker, seeded.tenant_a)
    assert "mcp_server.registered" in actions
    assert "mcp_server.tested" in actions
    assert "mcp_server.deleted" in actions


# --- AC-2: the stored credential is NEVER returned --------------------------


async def test_secret_value_is_never_returned_by_any_endpoint(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register with a bearer credential; NO endpoint echoes the value (only a hint)."""
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)

        resp = await _register(client, token, auth={"type": "bearer", "value": _BEARER_SECRET})
        assert resp.status_code == 201, resp.text
        created = resp.json()
        server_id = created["id"]

        # The 201 body: a masked hint, and NOT the value.
        assert created["secret_hint"]
        assert created["secret_hint"] != _BEARER_SECRET
        assert _BEARER_SECRET not in resp.text

        # Every read/mutate surface: the value never appears in the response body.
        surfaces = [
            await client.get(f"/api/v1/mcp-servers/{server_id}", headers=_auth(token)),
            await client.get("/api/v1/mcp-servers", headers=_auth(token)),
            await client.post(f"/api/v1/mcp-servers/{server_id}/test", headers=_auth(token)),
            await client.get(f"/api/v1/mcp-servers/{server_id}/tools", headers=_auth(token)),
            await client.patch(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "renamed"},
                headers=_auth(token),
            ),
        ]
        for resp in surfaces:
            assert resp.status_code == 200, resp.text
            assert _BEARER_SECRET not in resp.text, "a response leaked the stored secret value"

        # The hint is still present (masked) — auth stored, value hidden.
        got = await client.get(f"/api/v1/mcp-servers/{server_id}", headers=_auth(token))
        hint = got.json()["secret_hint"]
        assert hint and _BEARER_SECRET not in hint


async def test_rotating_and_clearing_the_credential_never_returns_it(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        resp = await _register(client, token, auth={"type": "bearer", "value": _BEARER_SECRET})
        server_id = resp.json()["id"]

        # rotate → a new masked hint, never the value
        rotated_value = "rotated-secret-value-abcdef"
        resp = await client.patch(
            f"/api/v1/mcp-servers/{server_id}",
            json={"auth": {"type": "bearer", "value": rotated_value}},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert rotated_value not in resp.text
        hint = resp.json()["secret_hint"]
        assert hint and hint != rotated_value

        # clear (auth: null) → hint cleared (absent-or-null under exclude_none), no value
        resp = await client.patch(
            f"/api/v1/mcp-servers/{server_id}",
            json={"auth": None},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("secret_hint") is None


# --- AC-3: SSRF / transport validation → 422 --------------------------------


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://127.0.0.1/mcp", "endpoint_blocked"),
        ("https://10.0.0.5/mcp", "endpoint_blocked"),
        ("https://169.254.169.254/mcp", "endpoint_blocked"),
        ("https://[::1]/mcp", "endpoint_blocked"),
        ("http://example.com/mcp", "endpoint_scheme"),  # non-https rejected
        ("ftp://example.com/mcp", "endpoint_scheme"),
    ],
)
async def test_register_blocked_or_non_https_endpoint_is_422(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    code: str,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        resp = await _register(client, token, url=url)
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == code


async def test_register_stdio_transport_is_422(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deferred / unsupported transport (stdio) is rejected at the boundary (ADR-0012 §1)."""
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        resp = await _register(client, token, transport="stdio")
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "unsupported_transport"


async def test_register_unknown_transport_is_422(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        resp = await _register(client, token, transport="carrier-pigeon")
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "unsupported_transport"


async def test_register_malformed_body_is_422(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        resp = await client.post(
            "/api/v1/mcp-servers",
            json={"transport": "streamable_http"},  # missing name + endpoint_url
            headers=_auth(token),
        )
        assert resp.status_code == 422


async def test_update_to_blocked_endpoint_is_422(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        server_id = (await _register(client, token)).json()["id"]
        resp = await client.patch(
            f"/api/v1/mcp-servers/{server_id}",
            json={"endpoint_url": "https://127.0.0.1/mcp"},
            headers=_auth(token),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "endpoint_blocked"


# --- §7: failure isolation — a down server is status=error, never a crash ---


async def test_test_a_down_server_is_status_error_not_a_crash(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        # A public-but-unreachable endpoint (DNS resolves, nothing serves it).
        resp = await _register(client, token, url=_DOWN_URL)
        server_id = resp.json()["id"]

        resp = await client.post(f"/api/v1/mcp-servers/{server_id}/test", headers=_auth(token))
        assert resp.status_code == 200, resp.text  # never an HTTP error (§7)
        body = resp.json()
        assert body["status"] == "error"
        assert body["last_error"]  # a safe failure reason
        assert body["discovered_tool_count"] == 0

    # A failed probe is still audited (INV-6).
    actions = await _audit_actions(sessionmaker, seeded.tenant_a)
    assert "mcp_server.tested" in actions


# --- AC-4: cross-tenant / cross-owner → 404 (INV-1/INV-2) --------------------


async def test_get_other_owner_is_404(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        bob_token = await _login(client, seeded.bob_email)
        bob_server_id = (await _register(client, bob_token)).json()["id"]

        alice_token = await _login(client, seeded.alice_email)
        for path in (
            f"/api/v1/mcp-servers/{bob_server_id}",
            f"/api/v1/mcp-servers/{bob_server_id}/tools",
        ):
            resp = await client.get(path, headers=_auth(alice_token))
            assert resp.status_code == 404, (path, resp.text)  # never 403


async def test_mutations_cross_tenant_are_404(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        alice_token = await _login(client, seeded.alice_email)
        server_id = (await _register(client, alice_token)).json()["id"]

        carol_token = await _login(client, seeded.carol_email)  # tenant B
        assert (
            await client.post(f"/api/v1/mcp-servers/{server_id}/test", headers=_auth(carol_token))
        ).status_code == 404
        assert (
            await client.patch(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "x"},
                headers=_auth(carol_token),
            )
        ).status_code == 404
        assert (
            await client.delete(f"/api/v1/mcp-servers/{server_id}", headers=_auth(carol_token))
        ).status_code == 404


async def test_list_is_owner_scoped(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        alice_token = await _login(client, seeded.alice_email)
        await _register(client, alice_token)

        bob_token = await _login(client, seeded.bob_email)
        await _register(client, bob_token)

        resp = await client.get("/api/v1/mcp-servers", headers=_auth(alice_token))
        assert len(resp.json()["items"]) == 1


async def test_missing_server_is_404(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        token = await _login(client, seeded.alice_email)
        resp = await client.post(f"/api/v1/mcp-servers/{uuid.uuid4()}/test", headers=_auth(token))
        assert resp.status_code == 404


# --- INV-4: auth ------------------------------------------------------------


async def test_unauthenticated_list_is_401(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        resp = await client.get("/api/v1/mcp-servers")
        assert resp.status_code == 401


async def test_malformed_bearer_is_401(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _offline(sessionmaker, monkeypatch) as (client, _limiter):
        resp = await client.get(
            "/api/v1/mcp-servers", headers={"Authorization": "Bearer not.a.jwt"}
        )
        assert resp.status_code == 401


# --- adapter seam sanity ----------------------------------------------------


async def test_transport_client_factory_binds_the_inner_transport() -> None:
    """The test factory pins the injected transport onto the built client (offline seam)."""
    async with fixture_mcp() as fx:
        factory = build_transport_client_factory(fx.inner_transport)

        async def _resolver(_ref: str) -> str | None:
            return None

        client = factory(
            McpClientParams(
                auth_resolver=_resolver,
                connect_timeout_seconds=5.0,
                call_timeout_seconds=5.0,
                user_agent="test/1",
                allowed_transports=frozenset(),
                endpoint_allowlist=frozenset(),
                rate_limit=lambda: True,
            )
        )
        assert client._inner_transport is fx.inner_transport  # noqa: SLF001
