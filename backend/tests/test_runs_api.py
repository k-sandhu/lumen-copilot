"""Runs API tests — the frozen ``/runs`` contract (list + detail, ADR-0015 §8, #235).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (no
Postgres / Redis / model), mirroring the chat/assistants API tests. Covers the two
frozen read routes:

* ``GET /runs`` — the inbox: the caller's own runs (newest first), filterable by
  ``assistant_id`` / ``schedule_id`` / ``status``, paginated;
* ``GET /runs/{runId}`` — run detail: status, inputs, transcript (``steps``),
  grounded citations, typed error;

plus the mandatory negatives: a cross-tenant / non-owned run id → **404** (INV-1/
INV-2, existence non-disclosure); no/bad token → **401** (INV-4).

Runs are seeded directly through the repository (the create path is the Celery task
/ scheduler, out of these read routes' scope), so the fixture stands up runs in two
tenants + a second owner to prove the isolation.
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
    RunRepository,
    RunStepRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    KnowledgeScope,
    Role,
    RunError,
    RunStatus,
    RunStepKind,
    RunTrigger,
)
from app.main import create_app
from app.services.assistants_service import config_from_assistant

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
        assistant_id: uuid.UUID,
        alice_run: uuid.UUID,
        alice_run_failed: uuid.UUID,
        alice_run_escalated: uuid.UUID,
        bob_run: uuid.UUID,
        bob_run_escalated: uuid.UUID,
        carol_run: uuid.UUID,
        carol_run_escalated: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.alice_email = "alice@acme.test"
        self.bob_email = "bob@acme.test"
        self.carol_email = "carol@globex.test"
        self.assistant_id = assistant_id
        self.alice_run = alice_run
        self.alice_run_failed = alice_run_failed
        self.alice_run_escalated = alice_run_escalated
        self.bob_run = bob_run
        self.bob_run_escalated = bob_run_escalated
        self.carol_run = carol_run
        self.carol_run_escalated = carol_run_escalated


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
            # A published assistant owned by alice, pinned to one version.
            assistants = AssistantRepository(seed, ta.id)
            assistant = await assistants.create(
                owner_id=alice.id,
                name="Weekly summary",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=bob.id,
            )
            await assistants.update(assistant.id, fields={"status": AssistantStatus.PUBLISHED})
            published = await assistants.get(assistant.id)
            version = await AssistantVersionRepository(seed, ta.id).add(
                assistant_id=assistant.id, version=1, author_id=alice.id,
                config=config_from_assistant(published),
            )
            # A Globex assistant so carol's run pins a version in her tenant.
            b_assistants = AssistantRepository(seed, tb.id)
            b_assistant = await b_assistants.create(
                owner_id=carol.id, name="Globex", knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(), autonomy_level=AutonomyLevel.SUGGEST, backup_owner_id=None,
            )

            runs_a = RunRepository(seed, ta.id)
            steps_a = RunStepRepository(seed, ta.id)
            # Alice: a succeeded run with a transcript, and a failed run.
            alice_run = await runs_a.create(
                owner_id=alice.id, assistant_id=assistant.id,
                assistant_version_id=version.id, trigger=RunTrigger.MANUAL,
                inputs={"prompt": "Summarize Q3"},
            )
            await runs_a.mark_terminal(
                alice_run.id, status=RunStatus.SUCCEEDED,
                finished_at=alice_run.created_at, summary="Q3 summary ready.",
            )
            await steps_a.add(
                run_id=alice_run.id, seq=0, kind=RunStepKind.DELTA, payload={"text": "Q3 summary."}
            )
            alice_failed = await runs_a.create(
                owner_id=alice.id, assistant_id=assistant.id,
                assistant_version_id=version.id, trigger=RunTrigger.SCHEDULE,
            )
            await runs_a.mark_terminal(
                alice_failed.id, status=RunStatus.FAILED, finished_at=alice_failed.created_at,
                error=RunError(code="model_unavailable", message="The model was unavailable."),
            )
            # Alice: an ESCALATED run awaiting a human decision (resume/cancel/reroute).
            alice_escalated = await runs_a.create(
                owner_id=alice.id, assistant_id=assistant.id,
                assistant_version_id=version.id, trigger=RunTrigger.SCHEDULE,
            )
            await runs_a.mark_terminal(
                alice_escalated.id, status=RunStatus.ESCALATED,
                finished_at=alice_escalated.created_at,
                error=RunError(code="restricted_data", message="A human must decide."),
            )
            # Bob: a run in the same tenant, owned by a different user; + an escalated one.
            bob_run = await runs_a.create(
                owner_id=bob.id, assistant_id=assistant.id,
                assistant_version_id=version.id, trigger=RunTrigger.MANUAL,
            )
            bob_escalated = await runs_a.create(
                owner_id=bob.id, assistant_id=assistant.id,
                assistant_version_id=version.id, trigger=RunTrigger.MANUAL,
            )
            await runs_a.mark_terminal(
                bob_escalated.id, status=RunStatus.ESCALATED,
                finished_at=bob_escalated.created_at,
                error=RunError(code="ambiguous", message="A human must decide."),
            )
            # Carol: a run in another tenant; + an escalated one.
            runs_b = RunRepository(seed, tb.id)
            carol_run = await runs_b.create(
                owner_id=carol.id, assistant_id=b_assistant.id,
                assistant_version_id=None, trigger=RunTrigger.MANUAL,
            )
            carol_escalated = await runs_b.create(
                owner_id=carol.id, assistant_id=b_assistant.id,
                assistant_version_id=None, trigger=RunTrigger.MANUAL,
            )
            await runs_b.mark_terminal(
                carol_escalated.id, status=RunStatus.ESCALATED,
                finished_at=carol_escalated.created_at,
                error=RunError(code="tool_failed", message="A human must decide."),
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                tenant_a=ta.id, alice_id=alice.id, bob_id=bob.id, assistant_id=assistant.id,
                alice_run=alice_run.id, alice_run_failed=alice_failed.id,
                alice_run_escalated=alice_escalated.id,
                bob_run=bob_run.id, bob_run_escalated=bob_escalated.id,
                carol_run=carol_run.id, carol_run_escalated=carol_escalated.id,
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
    # Stub the resume/reroute Celery re-enqueue so no broker is touched.
    monkeypatch.setattr("app.api.v1.runs.enqueue_run", lambda run_id, tenant_id: None)
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


# --- GET /runs (the inbox) --------------------------------------------------


async def test_list_runs_returns_only_callers_runs(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/runs", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ids = {item["id"] for item in body["items"]}
    # Alice sees her two runs; not bob's (same tenant, other owner) nor carol's.
    assert str(seeded.alice_run) in ids
    assert str(seeded.alice_run_failed) in ids
    assert str(seeded.bob_run) not in ids
    assert str(seeded.carol_run) not in ids
    # List items carry the summary line but NOT the transcript (steps present on detail).
    item = next(i for i in body["items"] if i["id"] == str(seeded.alice_run))
    assert item["status"] == "succeeded"
    assert item["summary"] == "Q3 summary ready."
    assert "steps" not in item  # excluded-none on the list view


async def test_list_runs_filters_by_status(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/runs?status=failed", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {str(seeded.alice_run_failed)}


async def test_list_runs_filters_by_assistant(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(
        f"/api/v1/runs?assistant_id={seeded.assistant_id}", headers=_auth(token)
    )
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert ids == {
        str(seeded.alice_run),
        str(seeded.alice_run_failed),
        str(seeded.alice_run_escalated),
    }


# --- GET /runs/{runId} (detail) ---------------------------------------------


async def test_get_run_detail_has_transcript_and_status(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/runs/{seeded.alice_run}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(seeded.alice_run)
    assert body["status"] == "succeeded"
    assert body["trigger"] == "manual"
    assert body["inputs"] == {"prompt": "Summarize Q3"}
    # The transcript (steps) is present on the detail.
    assert len(body["steps"]) == 1
    assert body["steps"][0]["kind"] == "delta"


async def test_get_failed_run_detail_has_typed_error(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/runs/{seeded.alice_run_failed}", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == {"code": "model_unavailable", "message": "The model was unavailable."}


# --- Negatives: INV-1 / INV-2 / INV-4 --------------------------------------


async def test_cross_tenant_run_detail_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-1: a run in another tenant is 404 for the caller (existence non-disclosure)."""
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/runs/{seeded.carol_run}", headers=_auth(token))
    assert resp.status_code == 404


async def test_other_owner_run_detail_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-2: a run owned by another user in the same tenant is 404, never 403."""
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/runs/{seeded.bob_run}", headers=_auth(token))
    assert resp.status_code == 404


async def test_unknown_run_id_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get(f"/api/v1/runs/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404


async def test_runs_require_a_token(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-4: no bearer token → 401 on both routes."""
    assert (await client.get("/api/v1/runs")).status_code == 401
    assert (await client.get(f"/api/v1/runs/{seeded.alice_run}")).status_code == 401


async def test_runs_reject_a_bad_token(client: AsyncClient, seeded: _Seeded) -> None:
    resp = await client.get("/api/v1/runs", headers=_auth("not-a-real-token"))
    assert resp.status_code == 401


# --- Escalation handoff: resume / cancel / reroute (E7-5, #239) -------------


async def test_owner_resumes_escalated_run(client: AsyncClient, seeded: _Seeded) -> None:
    """AC-2: the owner resumes an escalated run — 202 + the run back to ``queued``."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.alice_run_escalated}/resume", headers=_auth(token)
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert "error" not in body  # the escalation reason is cleared


async def test_owner_cancels_escalated_run(client: AsyncClient, seeded: _Seeded) -> None:
    """AC-2: the owner cancels an escalated run — 200 + a permanent terminal + reason."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.alice_run_escalated}/cancel", headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "cancelled"


async def test_owner_reroutes_escalated_run(client: AsyncClient, seeded: _Seeded) -> None:
    """AC-2: the owner reroutes an escalated run to another owner — 202 + reassigned+queued."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.alice_run_escalated}/reroute",
        headers=_auth(token),
        json={"to_owner_id": str(seeded.bob_id)},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "queued"
    # The run is now bob's — alice no longer sees it (INV-2 ownership moved).
    assert (
        await client.get(f"/api/v1/runs/{seeded.alice_run_escalated}", headers=_auth(token))
    ).status_code == 404


# --- Negatives: 409 not-escalated, 404 non-owner/cross-tenant, 422 bad target ---


async def test_resume_non_escalated_run_is_409(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-8: resuming a run that is not escalated is an illegal transition → 409."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.alice_run_failed}/resume", headers=_auth(token)
    )
    assert resp.status_code == 409, resp.text


async def test_resume_other_owner_escalated_run_is_404(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """INV-2: alice cannot act on bob's escalated run (404, existence non-disclosure)."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.bob_run_escalated}/resume", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_cancel_cross_tenant_escalated_run_is_404(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """INV-1: alice cannot cancel carol's escalated run in another tenant (404)."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.carol_run_escalated}/cancel", headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_reroute_to_current_owner_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-8: a no-op reroute (to the current owner) is malformed → 422."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.alice_run_escalated}/reroute",
        headers=_auth(token),
        json={"to_owner_id": str(seeded.alice_id)},
    )
    assert resp.status_code == 422, resp.text


async def test_reroute_to_unknown_target_is_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-1: rerouting to a user not in the tenant is 404 (existence non-disclosure)."""
    token = await _login(client, seeded.alice_email)
    resp = await client.post(
        f"/api/v1/runs/{seeded.alice_run_escalated}/reroute",
        headers=_auth(token),
        json={"to_owner_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


async def test_escalation_actions_require_a_token(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-4: no bearer token → 401 on every escalation action route."""
    rid = seeded.alice_run_escalated
    assert (await client.post(f"/api/v1/runs/{rid}/resume")).status_code == 401
    assert (await client.post(f"/api/v1/runs/{rid}/cancel")).status_code == 401
    assert (
        await client.post(f"/api/v1/runs/{rid}/reroute", json={"to_owner_id": str(seeded.bob_id)})
    ).status_code == 401
