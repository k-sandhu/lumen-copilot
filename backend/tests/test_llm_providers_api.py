"""LLM providers API tests — the /admin/llm-providers contract + required negatives.

Drives the real FastAPI app end-to-end against an **offline** in-memory SQLite DB
(no Postgres) with model discovery pointed at an in-process ``httpx.MockTransport``
(no socket) — the real router/service/repository + the real CC-C secrets vault (the
dev cipher over SQLite) + the real audit sink run; only the outbound ``GET /models``
is mocked. The admin router is auto-discovered + admin-gated, so hitting
``/api/v1/admin/llm-providers`` exercises the full admin-gated path.

Covered (foundation PR ACs + mandatory negatives):

* admin creates a provider (discovery mocked → status ``ready`` + ``discovered_models``);
* a non-admin on every path → **403** (INV-5); no/bad bearer → **401** (INV-4);
* a discovery failure → status ``error`` + ``last_error``, HTTP **200** (never a 500);
* update re-discovers; refresh re-runs discovery; delete → 204;
* the API never returns the plaintext key (only ``secret_hint``);
* tenant-scoping: another tenant's provider id → **404** (never 403, INV-1);
* an unsupported provider type / non-https / SSRF-blocked base URL → **422** (INV-8).

The discovery HTTP client is injected by patching the router's
``build_llm_provider_service`` to pass a ``discovery_client`` bound to a
``MockTransport`` (mirrors how the mcp-servers test injects a fixture client factory).
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
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
from app.services import llm_providers_service as svc

_PASSWORD = "devpassword"

# The provider host + a PUBLIC IP its DNS is stubbed to (so the SSRF guard admits the
# connect and the MockTransport serves it). A blocked-target test uses a literal
# loopback/private/metadata base URL instead, which never reaches this stub.
_PROVIDER_HOST = "provider.example.com"
_BASE_URL = f"https://{_PROVIDER_HOST}/v1"
_PUBLIC_IP = "93.184.216.34"

_API_KEY = "sk-super-secret-key-value-1234567890"

# The OpenAI-compatible /models catalog the mock returns for the happy path.
_MODELS_OK = {
    "object": "list",
    "data": [
        {"id": "openai/gpt-4o", "name": "GPT-4o"},
        {"id": "meta-llama/llama-3-70b"},
    ],
}


class _Seeded:
    def __init__(self, *, tenant_a: uuid.UUID, tenant_b: uuid.UUID) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.admin_a_email = "admin@acme.test"  # tenant A admin under test
        self.member_a_email = "member@acme.test"  # tenant A, non-admin
        self.admin_b_email = "admin@globex.test"  # tenant B admin


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
                email="admin@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.ADMIN],
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
    """Resolve the provider host to a PUBLIC IP so the SSRF guard admits it.

    The guard still runs in full (range check + IP-pin); we only make this host look
    public so a connect reaches the in-process MockTransport. A blocked-target test
    uses a literal private/loopback/metadata base URL, which never reaches this stub.
    """
    real = socket.getaddrinfo

    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        if host == _PROVIDER_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]
        return real(host, *args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


class _DiscoveryRecorder:
    """A configurable /models mock: records requests, replies with a set payload."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.payload: object = _MODELS_OK

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"error": "nope"})
        return httpx.Response(200, json=self.payload)


@pytest.fixture
def discovery() -> _DiscoveryRecorder:
    return _DiscoveryRecorder()


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession],
    discovery: _DiscoveryRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    """The app with its DB session pointed at SQLite + discovery pinned to the mock.

    Patches the admin router's ``build_llm_provider_service`` to inject a
    ``discovery_client`` bound to an ``httpx.MockTransport`` — so the real service +
    the real SSRF guard run, but the outbound ``GET /models`` is served in-process (no
    socket). CC-C secrets, the repository, and the audit sink are all real.
    """
    real_build = svc.build_llm_provider_service

    def _patched(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        kwargs["discovery_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(discovery.handler)
        )
        return real_build(*args, **kwargs)

    monkeypatch.setattr("app.api.v1.admin.build_llm_provider_service", _patched)

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


async def _create(
    client: AsyncClient,
    token: str,
    *,
    base_url: str = _BASE_URL,
    provider_type: str = "openai_compatible",
    api_key: str | None = _API_KEY,
) -> httpx.Response:
    body: dict[str, object] = {
        "name": "OpenRouter",
        "provider_type": provider_type,
        "base_url": base_url,
    }
    if api_key is not None:
        body["api_key"] = api_key
    return await client.post("/api/v1/admin/llm-providers", json=body, headers=_auth(token))


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


# --- happy path: create → discover → list → update → refresh → delete -------


async def test_create_discovers_models_and_round_trips(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    discovery: _DiscoveryRecorder,
) -> None:
    token = await _login(client, seeded.admin_a_email)

    # create → 201; discovery ran → status ready + the two discovered models
    resp = await _create(client, token)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    provider_id = body["id"]
    assert body["status"] == "ready"
    assert body["provider_type"] == "openai_compatible"
    assert body["last_discovery_at"] is not None
    assert body.get("last_error") is None  # excluded under response_model_exclude_none
    ids = [m["id"] for m in body["discovered_models"]]
    assert ids == ["openai/gpt-4o", "meta-llama/llama-3-70b"]
    # The label is derived: an entry's name, else its id.
    labels = {m["id"]: m["label"] for m in body["discovered_models"]}
    assert labels["openai/gpt-4o"] == "GPT-4o"
    assert labels["meta-llama/llama-3-70b"] == "meta-llama/llama-3-70b"
    # Discovery hit {base_url}/models with the bearer key.
    assert str(discovery.requests[0].url).endswith("/v1/models")
    assert discovery.requests[0].headers.get("authorization") == f"Bearer {_API_KEY}"

    # list → the one provider (tenant-scoped)
    resp = await client.get("/api/v1/admin/llm-providers", headers=_auth(token))
    assert resp.status_code == 200
    assert [p["id"] for p in resp.json()["items"]] == [provider_id]

    # update (rename + toggle) does not re-discover; a base_url change would.
    resp = await client.patch(
        f"/api/v1/admin/llm-providers/{provider_id}",
        json={"name": "Renamed", "enabled": False},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"
    assert resp.json()["enabled"] is False
    assert resp.json()["status"] == "ready"

    # refresh → re-runs discovery
    before = len(discovery.requests)
    resp = await client.post(
        f"/api/v1/admin/llm-providers/{provider_id}/refresh", headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ready"
    assert len(discovery.requests) == before + 1

    # delete → 204, gone
    resp = await client.delete(f"/api/v1/admin/llm-providers/{provider_id}", headers=_auth(token))
    assert resp.status_code == 204
    resp = await client.get("/api/v1/admin/llm-providers", headers=_auth(token))
    assert resp.json()["items"] == []

    # INV-6: the lifecycle is audited.
    actions = await _audit_actions(sessionmaker, seeded.tenant_a)
    assert "llm_provider.created" in actions
    assert "llm_provider.discovered" in actions
    assert "llm_provider.updated" in actions
    assert "llm_provider.deleted" in actions


async def test_update_base_url_re_discovers(
    client: AsyncClient,
    seeded: _Seeded,
    discovery: _DiscoveryRecorder,
) -> None:
    token = await _login(client, seeded.admin_a_email)
    provider_id = (await _create(client, token)).json()["id"]

    before = len(discovery.requests)
    resp = await client.patch(
        f"/api/v1/admin/llm-providers/{provider_id}",
        json={"base_url": f"https://{_PROVIDER_HOST}/v2"},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_url"].endswith("/v2")
    # A retarget re-runs discovery.
    assert len(discovery.requests) == before + 1
    assert str(discovery.requests[-1].url).endswith("/v2/models")


# --- the stored API key is NEVER returned -----------------------------------


async def test_api_key_is_never_returned_by_any_endpoint(
    client: AsyncClient,
    seeded: _Seeded,
) -> None:
    token = await _login(client, seeded.admin_a_email)

    resp = await _create(client, token)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    provider_id = created["id"]

    # The 201 body: a masked hint, and NOT the value.
    assert created["secret_hint"]
    assert created["secret_hint"] != _API_KEY
    assert _API_KEY not in resp.text

    surfaces = [
        await client.get("/api/v1/admin/llm-providers", headers=_auth(token)),
        await client.post(
            f"/api/v1/admin/llm-providers/{provider_id}/refresh", headers=_auth(token)
        ),
        await client.patch(
            f"/api/v1/admin/llm-providers/{provider_id}",
            json={"name": "renamed"},
            headers=_auth(token),
        ),
    ]
    for resp in surfaces:
        assert resp.status_code == 200, resp.text
        assert _API_KEY not in resp.text, "a response leaked the stored API key"


async def test_rotating_and_clearing_the_key_never_returns_it(
    client: AsyncClient,
    seeded: _Seeded,
) -> None:
    token = await _login(client, seeded.admin_a_email)
    provider_id = (await _create(client, token)).json()["id"]

    rotated = "sk-rotated-key-value-abcdefghijklmnop"
    resp = await client.patch(
        f"/api/v1/admin/llm-providers/{provider_id}",
        json={"api_key": rotated},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert rotated not in resp.text
    hint = resp.json()["secret_hint"]
    assert hint and hint != rotated

    # clear (api_key: null) → hint cleared (absent-or-null under exclude_none)
    resp = await client.patch(
        f"/api/v1/admin/llm-providers/{provider_id}",
        json={"api_key": None},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json().get("secret_hint") is None


# --- discovery failure → status error + last_error, HTTP 200 (never a 500) --


async def test_discovery_failure_is_status_error_not_a_crash(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    discovery: _DiscoveryRecorder,
) -> None:
    discovery.status_code = 401  # the provider rejects the key
    token = await _login(client, seeded.admin_a_email)

    resp = await _create(client, token)
    assert resp.status_code == 201, resp.text  # create still succeeds
    body = resp.json()
    assert body["status"] == "error"
    assert body["last_error"]  # a safe failure reason
    assert body["discovered_models"] == []

    # A failed discovery is still audited (INV-6).
    actions = await _audit_actions(sessionmaker, seeded.tenant_a)
    assert "llm_provider.discovered" in actions


async def test_discovery_malformed_response_is_status_error(
    client: AsyncClient,
    seeded: _Seeded,
    discovery: _DiscoveryRecorder,
) -> None:
    discovery.payload = {"not": "the expected shape"}
    token = await _login(client, seeded.admin_a_email)
    resp = await _create(client, token)
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "error"
    assert resp.json()["last_error"]


# --- validation: unsupported type / non-https / SSRF-blocked → 422 ----------


@pytest.mark.parametrize(
    ("base_url", "code"),
    [
        ("https://127.0.0.1/v1", "base_url_blocked"),
        ("https://10.0.0.5/v1", "base_url_blocked"),
        ("https://169.254.169.254/v1", "base_url_blocked"),
        ("https://[::1]/v1", "base_url_blocked"),
        ("http://provider.example.com/v1", "base_url_scheme"),  # non-https
        ("ftp://provider.example.com/v1", "base_url_scheme"),
    ],
)
async def test_create_blocked_or_non_https_base_url_is_422(
    client: AsyncClient,
    seeded: _Seeded,
    base_url: str,
    code: str,
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await _create(client, token, base_url=base_url)
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == code


async def test_create_unsupported_provider_type_is_422(
    client: AsyncClient,
    seeded: _Seeded,
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await _create(client, token, provider_type="anthropic_native")
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "unsupported_provider_type"


async def test_create_malformed_body_is_422(
    client: AsyncClient,
    seeded: _Seeded,
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.post(
        "/api/v1/admin/llm-providers",
        json={"provider_type": "openai_compatible"},  # missing name + base_url
        headers=_auth(token),
    )
    assert resp.status_code == 422


# --- INV-5: a non-admin is 403 on every path --------------------------------


async def test_non_admin_is_403_on_every_path(
    client: AsyncClient,
    seeded: _Seeded,
) -> None:
    member = await _login(client, seeded.member_a_email)
    fake = uuid.uuid4()
    calls = [
        await client.get("/api/v1/admin/llm-providers", headers=_auth(member)),
        await _create(client, member),
        await client.patch(
            f"/api/v1/admin/llm-providers/{fake}", json={"name": "x"}, headers=_auth(member)
        ),
        await client.delete(f"/api/v1/admin/llm-providers/{fake}", headers=_auth(member)),
        await client.post(f"/api/v1/admin/llm-providers/{fake}/refresh", headers=_auth(member)),
    ]
    for resp in calls:
        assert resp.status_code == 403, resp.text


# --- INV-1: cross-tenant is 404 (never 403) ---------------------------------


async def test_cross_tenant_provider_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    durable_audit_ledger,
) -> None:
    admin_a = await _login(client, seeded.admin_a_email)
    provider_id = (await _create(client, admin_a)).json()["id"]

    admin_b = await _login(client, seeded.admin_b_email)  # tenant B admin
    for resp in (
        await client.patch(
            f"/api/v1/admin/llm-providers/{provider_id}",
            json={"name": "x"},
            headers=_auth(admin_b),
        ),
        await client.delete(f"/api/v1/admin/llm-providers/{provider_id}", headers=_auth(admin_b)),
        await client.post(
            f"/api/v1/admin/llm-providers/{provider_id}/refresh", headers=_auth(admin_b)
        ),
    ):
        assert resp.status_code == 404, resp.text  # never 403

    assert [event.metadata["attempted_action"] for event in durable_audit_ledger.events] == [
        "llm_provider.update",
        "llm_provider.delete",
        "llm_provider.refresh",
    ]

    # And tenant B's list never shows tenant A's provider.
    resp = await client.get("/api/v1/admin/llm-providers", headers=_auth(admin_b))
    assert resp.json()["items"] == []


async def test_missing_provider_is_404(
    client: AsyncClient,
    seeded: _Seeded,
) -> None:
    token = await _login(client, seeded.admin_a_email)
    resp = await client.post(
        f"/api/v1/admin/llm-providers/{uuid.uuid4()}/refresh", headers=_auth(token)
    )
    assert resp.status_code == 404


# --- INV-4: auth ------------------------------------------------------------


async def test_unauthenticated_list_is_401(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/admin/llm-providers")
    assert resp.status_code == 401


async def test_malformed_bearer_is_401(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/admin/llm-providers", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert resp.status_code == 401
