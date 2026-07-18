"""Managed-connector OAuth tests — the F-CB-1 flow + negatives (#452, ADR-0019 §1).

Drives the real FastAPI app end-to-end against an offline in-memory SQLite DB,
with the provider faked at two seams: a **fake managed connector** (registered as
``gdrive`` by monkeypatching the registry lookups the services import) and a
**MockTransport token endpoint** (no network). The state store is the in-memory
:class:`InMemoryOAuthStateStore` (same contract as Redis, incl. single-use
consume).

Covered (the issue's acceptance criteria):

* happy path: admin create (``pending_auth``) → connect (consent URL carries the
  opaque state + S256 challenge, never the verifier) → callback (302
  ``connect=ok``) → source ``pending`` + ``connected_account`` + vault secret +
  first-sync enqueue + ``source.connected`` / ``user.identity_attested`` /
  ``secret.created`` audits;
* INV-5: non-admin create/connect → 403 (audited ``permission.denied``); member
  sync / demoted-owner delete of a managed source → 403;
* INV-4/INV-8 fail-closed callbacks: missing/unknown/replayed state → ``expired``;
  superseded generation / demoted admin / deleted source → ``denied``; provider
  error or failed exchange → ``provider_error`` with **no secret written**;
* 409s: connect on a non-OAuth type → ``oauth_not_supported``; sync while
  ``pending_auth`` → ``source_pending_auth``;
* wire discipline: responses validate against the frozen contract schemas; token
  material never appears in any response or stored config;
* attestation: admin attest → 200 + audit; non-admin → 403; unknown member → 404;
* lifecycle: deleting a managed source deletes its vault secret (audited).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jsonschema
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import (
    get_db_session,
    get_oauth_state_store_dep,
    get_oauth_token_http,
    get_object_store_dep,
)
from app.auth import hash_password
from app.connectors.base import ConnectorConfigError, ConnectorHealth, ConnectorRun
from app.connectors.oauth import InMemoryOAuthStateStore, OAuthSpec
from app.connectors.registry import UnknownConnectorError
from app.connectors.web import CONNECTOR as WEB_CONNECTOR
from app.db import models
from app.db.base import Base
from app.db.repositories import TenantRepository, UserRepository
from app.domain.entities import Role, Source
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PASSWORD = "devpassword"
_TOKEN_URL = "https://provider.test/token"
_AUTH_URL = "https://provider.test/auth"
_REFRESH_TOKEN = "rt-supersecret-refresh-0001"
_ACCESS_TOKEN = "at-supersecret-access-0002"

_SPEC = Path(__file__).resolve().parent.parent.parent / "contracts" / "openapi.yaml"


@pytest.fixture(scope="module")
def contract_schemas() -> dict[str, object]:
    spec = yaml.safe_load(_SPEC.read_text(encoding="utf-8"))
    return dict(spec["components"]["schemas"])


def _validate(payload: object, name: str, schemas: dict[str, object]) -> None:
    jsonschema.validate(payload, {**schemas[name], "components": {"schemas": schemas}})  # type: ignore[dict-item]


class FakeDriveConnector:
    """A managed OAuth connector double (what F-CB-2's gdrive will implement)."""

    name = "gdrive"

    def __init__(self) -> None:
        self.account_email: str | None = "alice@acme.test"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        mode = config.get("mode")
        if mode not in {"my_drive", "folder", "shared_drive"}:
            raise ConnectorConfigError("bad gdrive mode", code="invalid_config")
        return dict(config)

    async def sync(self, source: Source, run: ConnectorRun) -> list[object]:
        return []

    async def health(self, source: Source, run: ConnectorRun) -> ConnectorHealth:
        return ConnectorHealth(healthy=True)

    def oauth_spec(self) -> OAuthSpec:
        return OAuthSpec(
            authorize_url=_AUTH_URL,
            token_url=_TOKEN_URL,
            scopes=("drive.readonly",),
            client_id="client-id-1",
            client_secret="client-secret-1",
            allowed_hosts=("provider.test",),
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        )

    async def fetch_account_email(self, http: httpx.AsyncClient) -> str | None:
        # ADR-0019 §4: the probe receives a framework-built AUTHENTICATED client
        # — never the token string. The bearer rides the client's defaults.
        assert http.headers.get("Authorization") == f"Bearer {_ACCESS_TOKEN}"
        return self.account_email


class TokenEndpoint:
    """The faked provider token endpoint (MockTransport handler), per-test tunable."""

    def __init__(self) -> None:
        self.mode = "ok"  # ok | error | invalid_grant | no_refresh
        self.requests: list[dict[str, list[str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == _TOKEN_URL
        form = parse_qs(request.content.decode("utf-8"))
        self.requests.append(form)
        if self.mode == "error":
            return httpx.Response(500, json={"error": "server_error"})
        if self.mode == "invalid_grant":
            return httpx.Response(400, json={"error": "invalid_grant"})
        body = {
            "access_token": _ACCESS_TOKEN,
            "refresh_token": _REFRESH_TOKEN,
            "scope": "drive.readonly",
            "expires_in": 3600,
        }
        if self.mode == "no_refresh":
            body.pop("refresh_token")
        return httpx.Response(200, json=body)


class _Seeded:
    def __init__(self, *, tenant_a: uuid.UUID, tenant_b: uuid.UUID) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_email = "alice@acme.test"  # tenant A ADMIN under test
        self.bob_email = "bob@acme.test"  # tenant A member
        self.dave_email = "dave@acme.test"  # tenant A, a SECOND admin
        self.carol_email = "carol@globex.test"  # tenant B admin


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
                email="alice@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.ADMIN],
            )
            await UserRepository(seed, tenant_a.id).create(
                email="bob@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await UserRepository(seed, tenant_a.id).create(
                email="dave@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.ADMIN],
            )
            await UserRepository(seed, tenant_b.id).create(
                email="carol@globex.test",
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


@pytest.fixture
def fake_connector() -> FakeDriveConnector:
    return FakeDriveConnector()


@pytest.fixture(autouse=True)
def registry(
    monkeypatch: pytest.MonkeyPatch, fake_connector: FakeDriveConnector
) -> FakeDriveConnector:
    """Serve the fake managed connector as ``gdrive`` at every lookup seam."""

    def _get(source_type: str) -> object:
        if source_type == "web":
            return WEB_CONNECTOR
        if source_type == "gdrive":
            return fake_connector
        raise UnknownConnectorError(source_type)

    import importlib

    # NOTE: the attribute ``app.tasks.sync_source`` is the Celery task object
    # re-exported on ``app.tasks`` (shadowing the module of the same name), so
    # both the string form and ``import ... as`` resolve to the task. Fetch the
    # real module from the import system instead.
    sync_source_module = importlib.import_module("app.tasks.sync_source")

    monkeypatch.setattr("app.services.sources_service.get_connector", _get)
    monkeypatch.setattr("app.services.connector_oauth_service.get_connector", _get)
    monkeypatch.setattr(sync_source_module, "get_connector", _get)
    return fake_connector


@pytest.fixture(autouse=True)
def no_broker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, uuid.UUID]]:
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr("app.tasks.enqueue_source_sync", lambda tid, sid: calls.append((tid, sid)))
    monkeypatch.setattr("app.services.sources_service._dispatch_off_loop", lambda fn, *, name: fn())
    return calls


@pytest.fixture
def token_endpoint() -> TokenEndpoint:
    return TokenEndpoint()


@pytest.fixture
def state_store() -> InMemoryOAuthStateStore:
    return InMemoryOAuthStateStore()


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession],
    token_endpoint: TokenEndpoint,
    state_store: InMemoryOAuthStateStore,
) -> Iterator[FastAPI]:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    async def _override_token_http() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(token_endpoint.handler)
        ) as client:
            yield client

    class _NullStore:
        async def delete(self, tenant_id: str, key: str) -> None: ...

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_oauth_state_store_dep] = lambda: state_store
    application.dependency_overrides[get_oauth_token_http] = _override_token_http
    application.dependency_overrides[get_object_store_dep] = lambda: _NullStore()
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


async def _create_gdrive(client: AsyncClient, token: str) -> httpx.Response:
    return await client.post(
        "/api/v1/sources",
        json={"type": "gdrive", "config": {"mode": "my_drive"}},
        headers=_auth(token),
    )


async def _connect(client: AsyncClient, token: str, source_id: str) -> httpx.Response:
    return await client.post(f"/api/v1/sources/{source_id}/connect", headers=_auth(token))


def _state_of(authorization_url: str) -> str:
    query = parse_qs(urlsplit(authorization_url).query)
    return query["state"][0]


async def _audit_actions(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[str]:
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(models.AuditEvent).where(models.AuditEvent.tenant_id == tenant_id)
                )
            )
            .scalars()
            .all()
        )
        return [r.action for r in rows]


async def _secret_rows(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> list[models.Secret]:
    async with sessionmaker() as session:
        return list((await session.execute(select(models.Secret))).scalars().all())


# --- happy path --------------------------------------------------------------


async def test_full_connect_flow(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    no_broker: list[tuple[uuid.UUID, uuid.UUID]],
    contract_schemas: dict[str, object],
) -> None:
    token = await _login(client, seeded.alice_email)

    # Create: 201, pending_auth, contract-valid gdrive branch, no enqueue yet.
    resp = await _create_gdrive(client, token)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    _validate(body, "Source", contract_schemas)
    assert body["status"] == "pending_auth"
    assert body["reauthorize_required"] is False
    assert body["connected_account"] is None
    assert no_broker == []
    source_id = body["id"]

    # Connect: consent URL carries the opaque state + S256 challenge — never a
    # verifier, never a secret.
    resp = await _connect(client, token, source_id)
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorization_url"]
    assert url.startswith(_AUTH_URL)
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    assert "code_verifier" not in query
    assert "client-secret-1" not in url
    state = _state_of(url)

    # Callback: 302 connect=ok; the source is pending with the account bound.
    resp = await client.get(
        "/api/v1/sources/oauth/callback",
        params={"state": state, "code": "auth-code-1"},
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "connect=ok" in location and source_id in location
    assert _REFRESH_TOKEN not in location and _ACCESS_TOKEN not in location

    listed = await client.get("/api/v1/sources", headers=_auth(token))
    item = next(s for s in listed.json()["items"] if s["id"] == source_id)
    _validate(item, "Source", contract_schemas)
    assert item["status"] == "pending"
    assert item["connected_account"] == {"email": "alice@acme.test"}
    assert item["reauthorize_required"] is False

    # First sync enqueued; refresh token in the vault; nothing token-shaped on
    # the wire or in the stored config.
    assert no_broker == [(seeded.tenant_a, uuid.UUID(source_id))]
    secrets = await _secret_rows(sessionmaker)
    assert len(secrets) == 1 and secrets[0].kind == "connector_oauth"
    assert _REFRESH_TOKEN.encode() not in secrets[0].ciphertext  # encrypted, not stored raw
    assert _REFRESH_TOKEN not in json.dumps(listed.json())
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(models.Source).where(models.Source.id == uuid.UUID(source_id))
            )
        ).scalar_one()
        assert row.auth_secret_ref == secrets[0].id
        assert _REFRESH_TOKEN not in json.dumps(row.config)

    # Audits: connect + provider-verified auto-attestation + vault write (INV-6).
    actions = await _audit_actions(sessionmaker, seeded.tenant_a)
    assert "source.connected" in actions
    assert "user.identity_attested" in actions
    assert "secret.created" in actions


# --- INV-5: action-time admin gating ----------------------------------------


async def test_member_create_gdrive_is_403_and_audited(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.bob_email)
    resp = await _create_gdrive(client, token)
    assert resp.status_code == 403
    assert "permission.denied" in await _audit_actions(sessionmaker, seeded.tenant_a)


async def test_member_connect_is_403(client: AsyncClient, seeded: _Seeded) -> None:
    admin = await _login(client, seeded.alice_email)
    source_id = (await _create_gdrive(client, admin)).json()["id"]
    member = await _login(client, seeded.bob_email)
    resp = await _connect(client, member, source_id)
    assert resp.status_code == 403


async def test_member_sync_and_delete_of_managed_are_403(
    client: AsyncClient, seeded: _Seeded
) -> None:
    admin = await _login(client, seeded.alice_email)
    source_id = (await _create_gdrive(client, admin)).json()["id"]
    member = await _login(client, seeded.bob_email)
    resp = await client.post(f"/api/v1/sources/{source_id}/sync", headers=_auth(member))
    assert resp.status_code == 403
    resp = await client.delete(f"/api/v1/sources/{source_id}", headers=_auth(member))
    assert resp.status_code == 403


async def test_demoted_owner_loses_managed_mutations(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    admin = await _login(client, seeded.alice_email)
    source_id = (await _create_gdrive(client, admin)).json()["id"]
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(models.User).where(models.User.email == seeded.alice_email)
            )
        ).scalar_one()
        row.roles = [Role.MEMBER.value]
        await session.commit()
    demoted = await _login(client, seeded.alice_email)
    assert (await _connect(client, demoted, source_id)).status_code == 403
    assert (
        await client.post(f"/api/v1/sources/{source_id}/sync", headers=_auth(demoted))
    ).status_code == 403
    assert (
        await client.delete(f"/api/v1/sources/{source_id}", headers=_auth(demoted))
    ).status_code == 403


# --- 409s --------------------------------------------------------------------


async def test_connect_on_web_source_is_409(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/sources",
        json={"type": "web", "url": "http://93.184.216.34/x"},
        headers=_auth(token),
    )
    source_id = resp.json()["id"]
    resp = await _connect(client, token, source_id)
    assert resp.status_code == 409
    assert resp.json()["code"] == "oauth_not_supported"


async def test_sync_pending_auth_is_409(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    source_id = (await _create_gdrive(client, token)).json()["id"]
    resp = await client.post(f"/api/v1/sources/{source_id}/sync", headers=_auth(token))
    assert resp.status_code == 409
    assert resp.json()["code"] == "source_pending_auth"


async def test_connect_unknown_or_foreign_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await _connect(client, token, str(uuid.uuid4()))
    assert resp.status_code == 404
    source_id = (await _create_gdrive(client, token)).json()["id"]
    carol = await _login(client, seeded.carol_email)
    resp = await _connect(client, carol, source_id)
    assert resp.status_code == 404  # cross-tenant: existence non-disclosure (INV-1)


# --- fail-closed callbacks ---------------------------------------------------


async def _connected_state(client: AsyncClient, token: str) -> tuple[str, str]:
    source_id = (await _create_gdrive(client, token)).json()["id"]
    url = (await _connect(client, token, source_id)).json()["authorization_url"]
    return source_id, _state_of(url)


def _reason(resp: httpx.Response) -> str:
    assert resp.status_code == 302
    return parse_qs(urlsplit(resp.headers["location"]).query)["reason"][0]


async def test_callback_missing_or_unknown_state_is_expired(
    client: AsyncClient, seeded: _Seeded
) -> None:
    resp = await client.get("/api/v1/sources/oauth/callback", params={"code": "x"})
    assert _reason(resp) == "expired"
    resp = await client.get("/api/v1/sources/oauth/callback", params={"state": "nope", "code": "x"})
    assert _reason(resp) == "expired"


async def test_callback_replay_is_expired_and_single_use(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    source_id, state = await _connected_state(client, token)
    first = await client.get("/api/v1/sources/oauth/callback", params={"state": state, "code": "c"})
    assert "connect=ok" in first.headers["location"]
    replay = await client.get(
        "/api/v1/sources/oauth/callback", params={"state": state, "code": "c"}
    )
    assert _reason(replay) == "expired"


async def test_superseded_generation_is_denied(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    source_id = (await _create_gdrive(client, token)).json()["id"]
    first_url = (await _connect(client, token, source_id)).json()["authorization_url"]
    # Starting flow N+1 invalidates flow N even inside its TTL (ADR-0019 §1).
    await _connect(client, token, source_id)
    resp = await client.get(
        "/api/v1/sources/oauth/callback",
        params={"state": _state_of(first_url), "code": "c"},
    )
    assert _reason(resp) == "denied"


async def test_demoted_admin_at_callback_is_denied(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    _source_id, state = await _connected_state(client, token)
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(models.User).where(models.User.email == seeded.alice_email)
            )
        ).scalar_one()
        row.roles = [Role.MEMBER.value]
        await session.commit()
    resp = await client.get("/api/v1/sources/oauth/callback", params={"state": state, "code": "c"})
    assert _reason(resp) == "denied"
    assert not await _secret_rows(sessionmaker)


async def test_deleted_source_at_callback_is_denied(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    source_id, state = await _connected_state(client, token)
    resp = await client.delete(f"/api/v1/sources/{source_id}", headers=_auth(token))
    assert resp.status_code == 204
    resp = await client.get("/api/v1/sources/oauth/callback", params={"state": state, "code": "c"})
    assert _reason(resp) == "denied"
    assert not await _secret_rows(sessionmaker)


async def test_provider_error_and_failed_exchange_write_no_secret(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    token_endpoint: TokenEndpoint,
) -> None:
    token = await _login(client, seeded.alice_email)
    _sid, state = await _connected_state(client, token)
    resp = await client.get(
        "/api/v1/sources/oauth/callback",
        params={"state": state, "error": "access_denied"},
    )
    assert _reason(resp) == "provider_error"

    token_endpoint.mode = "error"
    _sid2, state2 = await _connected_state(client, token)
    resp = await client.get("/api/v1/sources/oauth/callback", params={"state": state2, "code": "c"})
    assert _reason(resp) == "provider_error"
    assert not await _secret_rows(sessionmaker)


async def test_no_refresh_token_on_first_connect_is_provider_error(
    client: AsyncClient,
    seeded: _Seeded,
    sessionmaker: async_sessionmaker[AsyncSession],
    token_endpoint: TokenEndpoint,
) -> None:
    token_endpoint.mode = "no_refresh"
    token = await _login(client, seeded.alice_email)
    _sid, state = await _connected_state(client, token)
    resp = await client.get("/api/v1/sources/oauth/callback", params={"state": state, "code": "c"})
    assert _reason(resp) == "provider_error"
    assert not await _secret_rows(sessionmaker)


# --- wire discipline ---------------------------------------------------------


async def test_web_source_wire_shape_is_pre_070(
    client: AsyncClient, seeded: _Seeded, contract_schemas: dict[str, object]
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/sources",
        json={"type": "web", "url": "http://93.184.216.34/x"},
        headers=_auth(token),
    )
    body = resp.json()
    _validate(body, "Source", contract_schemas)
    # The web branch is byte-compatible: no managed-health keys at all.
    assert "reauthorize_required" not in body
    assert "connected_account" not in body


async def test_gdrive_config_wire_shape_is_closed(
    client: AsyncClient, seeded: _Seeded, contract_schemas: dict[str, object]
) -> None:
    token = await _login(client, seeded.alice_email)
    body = (await _create_gdrive(client, token)).json()
    # my_drive: the closed variant carries no id keys, not id: null.
    assert body["config"] == {"mode": "my_drive"}
    _validate(body, "Source", contract_schemas)


# --- attestation -------------------------------------------------------------


async def test_admin_attests_member_identity(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    admin = await _login(client, seeded.alice_email)
    members = (await client.get("/api/v1/admin/members", headers=_auth(admin))).json()["items"]
    bob = next(m for m in members if m["email"] == seeded.bob_email)
    assert bob["email_attested_at"] is None

    resp = await client.post(
        f"/api/v1/admin/members/{bob['id']}/attest-identity", headers=_auth(admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email_attested_at"] is not None
    assert "user.identity_attested" in await _audit_actions(sessionmaker, seeded.tenant_a)


async def test_member_attest_is_403_unknown_member_is_404(
    client: AsyncClient, seeded: _Seeded
) -> None:
    member = await _login(client, seeded.bob_email)
    resp = await client.post(
        f"/api/v1/admin/members/{uuid.uuid4()}/attest-identity", headers=_auth(member)
    )
    assert resp.status_code == 403  # router gate (INV-5)
    admin = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/admin/members/{uuid.uuid4()}/attest-identity", headers=_auth(admin)
    )
    assert resp.status_code == 404  # unknown/foreign member (INV-1)


# --- lifecycle ---------------------------------------------------------------


async def test_callback_redirects_carry_no_store_and_no_referrer(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """The callback URL carried state/code — no cache, no Referer leak."""
    resp = await client.get("/api/v1/sources/oauth/callback", params={"code": "x"})
    assert resp.status_code == 302
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["referrer-policy"] == "no-referrer"


async def test_dockerfile_disables_uvicorn_access_log() -> None:
    """Smoke (ADR-0019 §1): uvicorn's access logger would print the callback
    query string — `state` and `code` — to stdout; the serve command must ship
    with it disabled."""
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
    assert "--no-access-log" in dockerfile


async def test_authenticated_client_is_host_pinned() -> None:
    """ADR-0019 §4: the guard rejects off-allowlist hosts BEFORE the bearer
    header could leave; allowed hosts pass through with the header attached."""
    from app.connectors.oauth import EgressNotAllowedError, build_authenticated_client

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={})

    spec = FakeDriveConnector().oauth_spec()
    async with build_authenticated_client(
        spec,
        access_token="tok",
        timeout=5.0,
        inner_transport=httpx.MockTransport(handler),
    ) as guarded:
        resp = await guarded.get("https://provider.test/about")
        assert resp.status_code == 200
        with pytest.raises(EgressNotAllowedError):
            await guarded.get("https://evil.example/exfiltrate")
    assert seen == ["https://provider.test/about"]  # the foreign host never dialled


async def test_refresh_invalid_grant_maps_to_dead_error(
    token_endpoint: TokenEndpoint,
) -> None:
    from app.connectors.oauth import OAuthGrantDeadError, refresh_access_token

    token_endpoint.mode = "invalid_grant"
    spec = FakeDriveConnector().oauth_spec()
    async with httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint.handler)) as http:
        with pytest.raises(OAuthGrantDeadError):
            await refresh_access_token(http, spec, refresh_token="rt-dead")


async def test_state_ttl_bounds_fail_fast() -> None:
    """Config guard: the TTL is bounded [1, 600] at boot (ADR-0019 §1)."""
    from pydantic import ValidationError as PydanticValidationError

    from app.core.config import Settings

    base = {
        "DATABASE_URL": "postgresql+asyncpg://t:t@localhost/t",
        "REDIS_URL": "redis://localhost",
        "CELERY_BROKER_URL": "redis://localhost",
        "CELERY_RESULT_BACKEND": "redis://localhost",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "t",
        "S3_SECRET_KEY": "tt",
        "S3_BUCKET": "b",
    }
    for bad in (0, -1, 601):
        with pytest.raises(PydanticValidationError):
            Settings(_env_file=None, **base, CONNECTOR_OAUTH_STATE_TTL_SECONDS=bad)  # type: ignore[arg-type]


async def test_interleaved_connect_generations_are_unique(
    seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession], client: AsyncClient
) -> None:
    """Two initiations through separate sessions can never mint the same
    generation — the increment is one atomic UPDATE, not read-then-add."""
    from app.db.repositories import SourceRepository

    token = await _login(client, seeded.alice_email)
    source_id = uuid.UUID((await _create_gdrive(client, token)).json()["id"])
    async with sessionmaker() as s1, sessionmaker() as s2:
        first = await SourceRepository(s1, seeded.tenant_a).begin_connect(source_id)
        await s1.commit()
        second = await SourceRepository(s2, seeded.tenant_a).begin_connect(source_id)
        await s2.commit()
    assert first is not None and second is not None
    assert {first.connect_generation, second.connect_generation} == {1, 2}


async def test_source_gone_syncing_between_check_and_finalize_loses_cas(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A source that starts syncing mid-flow loses the CAS: no credential is
    bound, no secret survives, the flow reports denied."""
    token = await _login(client, seeded.alice_email)
    source_id, state = await _connected_state(client, token)
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(models.Source).where(models.Source.id == uuid.UUID(source_id))
            )
        ).scalar_one()
        row.status = "syncing"
        await session.commit()
    resp = await client.get("/api/v1/sources/oauth/callback", params={"state": state, "code": "c"})
    assert _reason(resp) == "denied"
    assert not await _secret_rows(sessionmaker)


async def test_cross_admin_reconnect_rotates_secret_in_place(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ADR-0019 §1: a reconnect by a DIFFERENT admin rotates the credential
    under the SAME vault row — no per-owner duplicate, no orphan, and the
    source's stable reference is unchanged."""
    alice = await _login(client, seeded.alice_email)
    source_id, state = await _connected_state(client, alice)
    await client.get("/api/v1/sources/oauth/callback", params={"state": state, "code": "c"})
    before = await _secret_rows(sessionmaker)
    assert len(before) == 1
    original_id, original_ct = before[0].id, bytes(before[0].ciphertext)

    dave = await _login(client, seeded.dave_email)
    url = (await _connect(client, dave, source_id)).json()["authorization_url"]
    resp = await client.get(
        "/api/v1/sources/oauth/callback",
        params={"state": _state_of(url), "code": "c2"},
    )
    assert "connect=ok" in resp.headers["location"]

    after = await _secret_rows(sessionmaker)
    assert len(after) == 1, "cross-admin reconnect must not mint a second row"
    assert after[0].id == original_id
    assert bytes(after[0].ciphertext) != original_ct  # value rotated in place
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(models.Source).where(models.Source.id == uuid.UUID(source_id))
            )
        ).scalar_one()
        assert row.auth_secret_ref == original_id


async def test_delete_managed_source_deletes_vault_secret(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    source_id, state = await _connected_state(client, token)
    await client.get("/api/v1/sources/oauth/callback", params={"state": state, "code": "c"})
    assert len(await _secret_rows(sessionmaker)) == 1

    resp = await client.delete(f"/api/v1/sources/{source_id}", headers=_auth(token))
    assert resp.status_code == 204
    assert not await _secret_rows(sessionmaker)
    actions = await _audit_actions(sessionmaker, seeded.tenant_a)
    assert "secret.deleted" in actions and "source.deleted" in actions
