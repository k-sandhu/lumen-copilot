"""Code-runs API tests — the frozen ``GET /code-runs/{codeRunId}`` contract (ADR-0013 §4, #230).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (no
Postgres / Redis / model), mirroring the runs API tests. Covers the one frozen read
route (runs are agent-mediated — there is deliberately NO public execute endpoint):

* ``GET /code-runs/{codeRunId}`` — inspect one run: status, code, stdout/stderr,
  exit code, timing, resource usage, artifact ids;

plus the mandatory negatives (INV-1/INV-2 — existence non-disclosure): a cross-tenant
run id → **404**; a same-tenant non-owned run id → **404** (not 403); a malformed
(non-uuid) id → **422**; no/bad token → **401**.

Runs are seeded directly through the repository (the create path is the sandbox
Celery task / ``run_python`` tool, out of this read route's scope).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session
from app.auth import hash_password
from app.db.base import Base
from app.db.repositories import CodeRunRepository, TenantRepository, UserRepository
from app.domain.entities import CodeRunStatus, ResourceUsage, Role
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        alice_email: str,
        bob_email: str,
        carol_email: str,
        alice_run: uuid.UUID,
        bob_run: uuid.UUID,
        carol_run: uuid.UUID,
    ) -> None:
        self.alice_email = alice_email
        self.bob_email = bob_email
        self.carol_email = carol_email
        self.alice_run = alice_run
        self.bob_run = bob_run
        self.carol_run = carol_run


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
            runs_a = CodeRunRepository(seed, ta.id)
            alice_run = await runs_a.create(owner_id=alice.id, code="print('alice')")
            await runs_a.mark_terminal(
                alice_run.id,
                status=CodeRunStatus.SUCCEEDED,
                finished_at=datetime.now(UTC),
                stdout="alice\n",
                exit_code=0,
                duration_ms=42,
                resource_usage=ResourceUsage(peak_memory_bytes=2048, output_bytes=6),
                image_digest="sha256:abc",
            )
            bob_run = await runs_a.create(owner_id=bob.id, code="print('bob')")
            carol_run = await CodeRunRepository(seed, tb.id).create(
                owner_id=carol.id, code="print('carol')"
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                alice_email="alice@acme.test",
                bob_email="bob@acme.test",
                carol_email="carol@globex.test",
                alice_run=alice_run.id,
                bob_run=bob_run.id,
                carol_run=carol_run.id,
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


# --- Happy path -------------------------------------------------------------


async def test_get_own_code_run_returns_the_record(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/code-runs/{seeded.alice_run}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(seeded.alice_run)
    assert body["status"] == "succeeded"
    assert body["code"] == "print('alice')"
    assert body["stdout"] == "alice\n"
    assert body["exit_code"] == 0
    assert body["duration_ms"] == 42
    assert body["image_digest"] == "sha256:abc"
    assert body["artifact_ids"] == []
    assert body["resource_usage"]["peak_memory_bytes"] == 2048


# --- Negatives (INV-1/INV-2/INV-4) ------------------------------------------


async def test_cross_tenant_run_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-1: alice cannot read carol's (other-tenant) run — 404, existence non-disclosure."""
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/code-runs/{seeded.carol_run}", headers=_auth(token))
    assert resp.status_code == 404, resp.text


async def test_same_tenant_non_owner_run_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-2: alice cannot read bob's (same-tenant, other-owner) run — 404, not 403."""
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/code-runs/{seeded.bob_run}", headers=_auth(token))
    assert resp.status_code == 404, resp.text


async def test_unknown_run_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/code-runs/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404, resp.text


async def test_malformed_id_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/code-runs/not-a-uuid", headers=_auth(token))
    assert resp.status_code == 422, resp.text


async def test_missing_token_is_401(client: AsyncClient, seeded: _Seeded) -> None:
    resp = await client.get(f"/api/v1/code-runs/{seeded.alice_run}")
    assert resp.status_code == 401, resp.text
