"""Request-level reusable sandbox lifecycle coverage (ADR-0020, issue #457)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session, get_settings_dep
from app.auth import hash_password
from app.core.errors import DependencyError
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    CodeRunRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.main import create_app
from app.sandbox.spec import SandboxSessionSpec
from tests._sandbox_helpers import sandbox_settings

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


@dataclass(frozen=True)
class _Seeded:
    alice_email: str
    alice_chat: UUID
    bob_chat: UUID
    carol_chat: UUID
    tenant_id: UUID
    alice_id: UUID


class _FakeRunner:
    def __init__(self) -> None:
        self.ensured: list[SandboxSessionSpec] = []
        self.resets: list[tuple[SandboxSessionSpec, SandboxSessionSpec]] = []
        self.closed: list[SandboxSessionSpec] = []
        self.cancelled: list[tuple[SandboxSessionSpec, UUID]] = []
        self.reset_error: Exception | None = None

    async def ensure_session(self, value: SandboxSessionSpec) -> None:
        self.ensured.append(value)

    async def reset_session(
        self, previous: SandboxSessionSpec, replacement: SandboxSessionSpec
    ) -> None:
        self.resets.append((previous, replacement))
        if self.reset_error is not None:
            raise self.reset_error

    async def close_session(self, value: SandboxSessionSpec) -> None:
        self.closed.append(value)

    async def cancel(self, value: SandboxSessionSpec, execution_id: UUID) -> None:
        self.cancelled.append((value, execution_id))


@pytest_asyncio.fixture
async def sessionmaker_fixture() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            acme = await TenantRepository(session).create(name="Acme")
            globex = await TenantRepository(session).create(name="Globex")
            alice = await UserRepository(session, acme.id).create(
                email="alice@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            bob = await UserRepository(session, acme.id).create(
                email="bob@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            carol = await UserRepository(session, globex.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            alice_chat = await ChatSessionRepository(session, acme.id).create(
                owner_id=alice.id, model="openrouter:test/model"
            )
            bob_chat = await ChatSessionRepository(session, acme.id).create(
                owner_id=bob.id, model="openrouter:test/model"
            )
            carol_chat = await ChatSessionRepository(session, globex.id).create(
                owner_id=carol.id, model="openrouter:test/model"
            )
            await TenantSandboxPolicyRepository(session, acme.id).upsert(
                enabled=True,
                allowed_packages=("*",),
                denied_packages=(),
                egress_allowed=False,
                egress_allowlist=(),
                max_runtime_s=30,
                max_memory_mb=512,
                daily_runtime_cap_s=3600,
                max_concurrency=2,
                updated_by=alice.id,
            )
            await session.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                alice_email=alice.email,
                alice_chat=alice_chat.id,
                bob_chat=bob_chat.id,
                carol_chat=carol_chat.id,
                tenant_id=acme.id,
                alice_id=alice.id,
            )
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker_fixture: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker_fixture.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def fake_runner() -> _FakeRunner:
    return _FakeRunner()


@pytest.fixture
def app(
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
    fake_runner: _FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    application = create_app()
    settings = sandbox_settings()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker_fixture() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_settings_dep] = lambda: settings

    import app.api.v1.chat as chat_api
    import app.api.v1.code_runs as code_runs_api

    monkeypatch.setattr(chat_api, "HttpSandboxRunner", lambda _url: fake_runner)
    monkeypatch.setattr(code_runs_api, "HttpSandboxRunner", lambda _url: fake_runner)
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def _token(client: AsyncClient, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": _PASSWORD}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_reset_reuses_identity_and_advances_generation_through_api(
    client: AsyncClient, seeded: _Seeded, fake_runner: _FakeRunner
) -> None:
    token = await _token(client, seeded.alice_email)
    path = f"/api/v1/chat/sessions/{seeded.alice_chat}/sandbox"

    initial = await client.get(path, headers=_auth(token))
    assert initial.status_code == 200
    assert initial.json() == {
        "status": "not_created",
        "enabled": True,
        "root_access": True,
    }

    first = await client.post(f"{path}/reset", headers=_auth(token))
    second = await client.post(f"{path}/reset", headers=_auth(token))
    assert first.status_code == second.status_code == 200
    assert first.json()["sandbox_session_id"] == second.json()["sandbox_session_id"]
    assert first.json()["generation"] == 1
    assert second.json()["generation"] == 2
    assert first.json()["root_access"] is True
    assert len(fake_runner.ensured) == 1
    assert len(fake_runner.resets) == 1


async def test_close_is_idempotent_and_state_remains_inspectable(
    client: AsyncClient, seeded: _Seeded, fake_runner: _FakeRunner
) -> None:
    token = await _token(client, seeded.alice_email)
    path = f"/api/v1/chat/sessions/{seeded.alice_chat}/sandbox"
    assert (await client.post(f"{path}/reset", headers=_auth(token))).status_code == 200

    first = await client.delete(path, headers=_auth(token))
    second = await client.delete(path, headers=_auth(token))
    current = await client.get(path, headers=_auth(token))
    assert first.status_code == second.status_code == 204
    assert current.json()["status"] == "closed"
    assert len(fake_runner.closed) == 1


async def test_failed_reset_commits_error_generation_and_can_recover(
    client: AsyncClient, seeded: _Seeded, fake_runner: _FakeRunner
) -> None:
    token = await _token(client, seeded.alice_email)
    path = f"/api/v1/chat/sessions/{seeded.alice_chat}/sandbox"
    assert (await client.post(f"{path}/reset", headers=_auth(token))).status_code == 200
    fake_runner.reset_error = DependencyError("runner unavailable")

    failed = await client.post(f"{path}/reset", headers=_auth(token))
    current = await client.get(path, headers=_auth(token))
    assert failed.status_code == 503
    assert current.json()["status"] == "error"
    assert current.json()["generation"] == 2

    fake_runner.reset_error = None
    recovered = await client.post(f"{path}/reset", headers=_auth(token))
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "active"
    assert recovered.json()["generation"] == 3


async def test_other_owner_and_cross_tenant_chat_are_both_404(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _token(client, seeded.alice_email)
    for chat_id in (seeded.bob_chat, seeded.carol_chat):
        response = await client.get(
            f"/api/v1/chat/sessions/{chat_id}/sandbox", headers=_auth(token)
        )
        assert response.status_code == 404


async def test_deleting_chat_closes_active_runner_session_first(
    client: AsyncClient, seeded: _Seeded, fake_runner: _FakeRunner
) -> None:
    token = await _token(client, seeded.alice_email)
    sandbox_path = f"/api/v1/chat/sessions/{seeded.alice_chat}/sandbox"
    assert (await client.post(f"{sandbox_path}/reset", headers=_auth(token))).status_code == 200

    deleted = await client.delete(
        f"/api/v1/chat/sessions/{seeded.alice_chat}", headers=_auth(token)
    )

    assert deleted.status_code == 204
    assert len(fake_runner.closed) == 1
    assert (await client.get(sandbox_path, headers=_auth(token))).status_code == 404


async def test_cancel_kills_run_and_advances_environment_generation(
    client: AsyncClient,
    seeded: _Seeded,
    fake_runner: _FakeRunner,
    sessionmaker_fixture: async_sessionmaker[AsyncSession],
) -> None:
    token = await _token(client, seeded.alice_email)
    sandbox_path = f"/api/v1/chat/sessions/{seeded.alice_chat}/sandbox"
    created = await client.post(f"{sandbox_path}/reset", headers=_auth(token))
    sandbox = created.json()
    async with sessionmaker_fixture() as session:
        runs = CodeRunRepository(session, seeded.tenant_id)
        run = await runs.create(
            owner_id=seeded.alice_id,
            session_id=seeded.alice_chat,
            code="while True: pass",
        )
        await runs.mark_running(
            run.id,
            started_at=datetime.now(UTC),
            image_digest="lumen-sandbox-runner:0.2.0",
            sandbox_session_id=UUID(sandbox["sandbox_session_id"]),
            sandbox_generation=1,
        )
        await session.commit()

    response = await client.post(
        f"/api/v1/code-runs/{run.id}/cancel", headers=_auth(token)
    )
    current = await client.get(sandbox_path, headers=_auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "killed"
    assert response.json()["stderr"] == "Code execution was cancelled."
    assert current.json()["generation"] == 2
    assert fake_runner.cancelled[0][1] == run.id
