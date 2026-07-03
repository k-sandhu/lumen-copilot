"""Schedules API tests — the frozen ``/schedules`` contract (ADR-0015 §8, #236).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (no
Postgres / Redis / model), mirroring the runs/assistants API tests. Covers the frozen
CRUD + control routes and the mandatory negatives:

* create/list/get/patch/delete a schedule;
* ``pause`` / ``resume`` (idempotent) and ``run-now`` (202 → ``run_id``);
* no/bad token → **401** (INV-4);
* cross-tenant / non-owned schedule id → **404** (INV-1/INV-2);
* malformed cron / unknown timezone → **422** (INV-8);
* ``run-now`` on a paused schedule → **409** (INV-8);
* scheduling a disabled assistant → **422** (the mandatory negative).

The run-now Celery enqueue is stubbed so no broker is touched.
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
    AssistantVersionRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    KnowledgeScope,
    Role,
)
from app.main import create_app
from app.services.assistants_service import config_from_assistant

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"
_NY = "America/New_York"


class _Seeded:
    def __init__(
        self,
        *,
        assistant_id: uuid.UUID,
        disabled_assistant_id: uuid.UUID,
        carol_assistant_id: uuid.UUID,
    ) -> None:
        self.alice_email = "alice@acme.test"
        self.bob_email = "bob@acme.test"
        self.carol_email = "carol@globex.test"
        self.assistant_id = assistant_id
        self.disabled_assistant_id = disabled_assistant_id
        self.carol_assistant_id = carol_assistant_id


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
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    async with sessionmaker() as seed:
        ta = await TenantRepository(seed).create(name="Acme")
        tb = await TenantRepository(seed).create(name="Globex")
        alice = await UserRepository(seed, ta.id).create(
            email="alice@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
        )
        bob = await UserRepository(seed, ta.id).create(
            email="bob@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
        )
        carol = await UserRepository(seed, tb.id).create(
            email="carol@globex.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
        )
        assistants = AssistantRepository(seed, ta.id)
        versions = AssistantVersionRepository(seed, ta.id)
        published = await assistants.create(
            owner_id=alice.id, name="Weekly", knowledge_scope=KnowledgeScope.empty(),
            tool_allowlist=(), autonomy_level=AutonomyLevel.SUGGEST, backup_owner_id=bob.id,
        )
        await assistants.update(published.id, fields={"status": AssistantStatus.PUBLISHED})
        head = await assistants.get(published.id)
        await versions.add(
            assistant_id=published.id, version=1, author_id=alice.id,
            config=config_from_assistant(head),
        )
        disabled = await assistants.create(
            owner_id=alice.id, name="Retired", knowledge_scope=KnowledgeScope.empty(),
            tool_allowlist=(), autonomy_level=AutonomyLevel.SUGGEST, backup_owner_id=bob.id,
        )
        await assistants.update(disabled.id, fields={"status": AssistantStatus.DISABLED})

        assistants_b = AssistantRepository(seed, tb.id)
        versions_b = AssistantVersionRepository(seed, tb.id)
        carol_ast = await assistants_b.create(
            owner_id=carol.id, name="Globex", knowledge_scope=KnowledgeScope.empty(),
            tool_allowlist=(), autonomy_level=AutonomyLevel.SUGGEST, backup_owner_id=None,
        )
        await assistants_b.update(carol_ast.id, fields={"status": AssistantStatus.PUBLISHED})
        head_b = await assistants_b.get(carol_ast.id)
        await versions_b.add(
            assistant_id=carol_ast.id, version=1, author_id=carol.id,
            config=config_from_assistant(head_b),
        )
        await seed.commit()
        return _Seeded(
            assistant_id=published.id,
            disabled_assistant_id=disabled.id,
            carol_assistant_id=carol_ast.id,
        )


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
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
    # Stub the run-now Celery enqueue so no broker is touched.
    monkeypatch.setattr("app.api.v1.schedules.enqueue_run", lambda run_id, tenant_id: None)
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
    client: AsyncClient, token: str, seeded: _Seeded, **overrides: object
) -> dict[str, object]:
    body: dict[str, object] = {
        "assistant_id": str(seeded.assistant_id),
        "cadence": {"cron": "0 8 * * *"},
        "timezone": _NY,
    }
    body.update(overrides)
    resp = await client.post("/api/v1/schedules", headers=_auth(token), json=body)
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# --- create -----------------------------------------------------------------


async def test_create_schedule_returns_next_run(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    body = await _create(client, token, seeded, input_params={"prompt": "Q3"})
    assert body["enabled"] is True
    assert body["next_run_at"] is not None
    assert body["overlap_policy"] == "skip"
    assert body["cadence"] == {"cron": "0 8 * * *"}


async def test_create_structured_cadence_round_trips(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    body = await _create(
        client, token, seeded,
        cadence={"structured": {"every": "week", "at": "09:30", "day_of_week": 1}},
    )
    assert body["cadence"]["structured"]["every"] == "week"
    assert body["cadence"]["structured"]["at"] == "09:30"


async def test_create_bad_cron_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/schedules", headers=_auth(token),
        json={"assistant_id": str(seeded.assistant_id), "cadence": {"cron": "not a cron"},
              "timezone": _NY},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_cron"


async def test_create_unknown_timezone_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/schedules", headers=_auth(token),
        json={"assistant_id": str(seeded.assistant_id), "cadence": {"cron": "0 8 * * *"},
              "timezone": "Not/AZone"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_timezone"


async def test_create_disabled_assistant_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    """Scheduling a disabled assistant is rejected 422 (the mandatory negative)."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/schedules", headers=_auth(token),
        json={"assistant_id": str(seeded.disabled_assistant_id), "cadence": {"cron": "0 8 * * *"},
              "timezone": _NY},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "assistant_not_runnable"


async def test_create_cross_tenant_assistant_is_404(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/schedules", headers=_auth(token),
        json={"assistant_id": str(seeded.carol_assistant_id), "cadence": {"cron": "0 8 * * *"},
              "timezone": _NY},
    )
    assert resp.status_code == 404, resp.text


async def test_create_cadence_requires_exactly_one_form(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        "/api/v1/schedules", headers=_auth(token),
        json={"assistant_id": str(seeded.assistant_id),
              "cadence": {"cron": "0 8 * * *", "structured": {"every": "day", "at": "08:00"}},
              "timezone": _NY},
    )
    assert resp.status_code == 422  # both forms present → contract oneOf violation


# --- auth negatives ---------------------------------------------------------


async def test_list_requires_auth(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/schedules")
    assert resp.status_code == 401


async def test_create_requires_auth(client: AsyncClient, seeded: _Seeded) -> None:
    resp = await client.post(
        "/api/v1/schedules",
        json={"assistant_id": str(seeded.assistant_id), "cadence": {"cron": "0 8 * * *"},
              "timezone": _NY},
    )
    assert resp.status_code == 401


# --- list / get / cross-tenant ---------------------------------------------


async def test_list_returns_only_callers_schedules(
    client: AsyncClient, seeded: _Seeded
) -> None:
    alice = await _login(client, seeded.alice_email)
    created = await _create(client, alice, seeded)
    bob = await _login(client, seeded.bob_email)
    resp = await client.get("/api/v1/schedules", headers=_auth(bob))
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert created["id"] not in ids  # bob does not see alice's schedule


async def test_list_filters_by_enabled(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    on = await _create(client, token, seeded)
    off = await _create(client, token, seeded, cadence={"cron": "0 9 * * *"}, enabled=False)
    resp = await client.get("/api/v1/schedules?enabled=true", headers=_auth(token))
    ids = {item["id"] for item in resp.json()["items"]}
    assert on["id"] in ids
    assert off["id"] not in ids


async def test_get_cross_tenant_schedule_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    alice = await _login(client, seeded.alice_email)
    created = await _create(client, alice, seeded)
    carol = await _login(client, seeded.carol_email)
    resp = await client.get(f"/api/v1/schedules/{created['id']}", headers=_auth(carol))
    assert resp.status_code == 404  # existence non-disclosure (INV-1)


async def test_get_unknown_schedule_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/schedules/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404


# --- patch ------------------------------------------------------------------


async def test_patch_updates_cadence(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded)
    resp = await client.patch(
        f"/api/v1/schedules/{created['id']}", headers=_auth(token),
        json={"cadence": {"cron": "30 6 * * *"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cadence"] == {"cron": "30 6 * * *"}


async def test_patch_empty_body_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded)
    resp = await client.patch(
        f"/api/v1/schedules/{created['id']}", headers=_auth(token), json={}
    )
    assert resp.status_code == 422  # minProperties: 1


async def test_patch_bad_timezone_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded)
    resp = await client.patch(
        f"/api/v1/schedules/{created['id']}", headers=_auth(token),
        json={"timezone": "Also/Bogus"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_timezone"


# --- delete -----------------------------------------------------------------


async def test_delete_then_get_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded)
    resp = await client.delete(f"/api/v1/schedules/{created['id']}", headers=_auth(token))
    assert resp.status_code == 204
    resp = await client.get(f"/api/v1/schedules/{created['id']}", headers=_auth(token))
    assert resp.status_code == 404


# --- pause / resume ---------------------------------------------------------


async def test_pause_then_resume(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded)
    paused = await client.post(
        f"/api/v1/schedules/{created['id']}/pause", headers=_auth(token)
    )
    assert paused.status_code == 200
    assert paused.json()["enabled"] is False
    assert paused.json().get("next_run_at") is None

    resumed = await client.post(
        f"/api/v1/schedules/{created['id']}/resume", headers=_auth(token)
    )
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True
    assert resumed.json()["next_run_at"] is not None


async def test_pause_is_idempotent(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded, enabled=False)
    resp = await client.post(f"/api/v1/schedules/{created['id']}/pause", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


# --- run-now ----------------------------------------------------------------


async def test_run_now_returns_202_and_run_id(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded)
    resp = await client.post(
        f"/api/v1/schedules/{created['id']}/run-now", headers=_auth(token)
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["run_id"]
    # The run is now visible in the caller's run inbox.
    runs = await client.get(f"/api/v1/runs?schedule_id={created['id']}", headers=_auth(token))
    ids = {item["id"] for item in runs.json()["items"]}
    assert run_id in ids


async def test_run_now_on_paused_schedule_is_409(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    created = await _create(client, token, seeded, enabled=False)
    resp = await client.post(
        f"/api/v1/schedules/{created['id']}/run-now", headers=_auth(token)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "schedule_paused"


async def test_run_now_cross_tenant_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    alice = await _login(client, seeded.alice_email)
    created = await _create(client, alice, seeded)
    carol = await _login(client, seeded.carol_email)
    resp = await client.post(
        f"/api/v1/schedules/{created['id']}/run-now", headers=_auth(carol)
    )
    assert resp.status_code == 404
