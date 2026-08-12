"""Assistant library-governance API tests — the /admin/assistants* surface (#217).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (no
Postgres / Redis / model), mirroring ``test_admin_api`` + ``test_assistants_api``.
Covers the additive ``contracts/openapi.yaml`` ``/admin/assistants*`` surface
(certify / feature / disable / transfer-ownership / list / disable-orphans) plus
the governance fields now on the owner-facing ``Assistant`` list responses.

Two tenants with a mix of roles are seeded so the role-gating (INV-5) and
tenant-scoping (INV-1) negatives are real. The MANDATORY negatives (issue #217
AC-N + spec 0004 §3):

* INV-5: a non-admin governance mutation → **403** (on every governance path);
* INV-8: a disabled assistant cannot start a chat / schedule / run (negative);
* INV-6: certify / disable / ownership-transfer each emit exactly one audit event;
* INV-1: a cross-tenant assistant id → **404** (existence non-disclosure).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_backplane_dep, get_db_session
from app.auth import hash_password
from app.db import models
from app.db.base import Base
from app.db.repositories import AuditEventRepository, TenantRepository, UserRepository
from app.domain.entities import CertificationState, Role
from app.main import create_app
from app.realtime.backplane import InMemoryBackplane
from app.services.assistant_governance_service import AssistantGovernanceService
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_recorder

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    """Identifiers for the seeded fixture graph (two tenants, mixed roles)."""

    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        admin_a: uuid.UUID,
        member_a: uuid.UUID,
        member2_a: uuid.UUID,
        admin_b: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.admin_a = admin_a
        self.member_a = member_a
        self.member2_a = member2_a
        self.admin_b = admin_b
        self.admin_a_email = "admin@acme.test"
        self.member_a_email = "member@acme.test"
        self.member2_a_email = "member2@acme.test"
        self.admin_b_email = "admin@globex.test"


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
            admin_a = await UserRepository(seed, ta.id).create(
                email="admin@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.ADMIN],
            )
            member_a = await UserRepository(seed, ta.id).create(
                email="member@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            member2_a = await UserRepository(seed, ta.id).create(
                email="member2@acme.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            admin_b = await UserRepository(seed, tb.id).create(
                email="admin@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.ADMIN],
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                tenant_a=ta.id,
                tenant_b=tb.id,
                admin_a=admin_a.id,
                member_a=member_a.id,
                member2_a=member2_a.id,
                admin_b=admin_b.id,
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
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_backplane_dep] = lambda: InMemoryBackplane()

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


async def _create_published_assistant(
    client: AsyncClient, owner_token: str, *, backup_owner: uuid.UUID, name: str = "Helper"
) -> str:
    """Create + publish an assistant as the owner; return its id."""
    created = await client.post(
        "/api/v1/assistants",
        headers=_auth(owner_token),
        json={"name": name, "backupOwner": str(backup_owner)},
    )
    assert created.status_code == 201, created.text
    aid = str(created.json()["id"])
    published = await client.post(
        f"/api/v1/assistants/{aid}/publish", headers=_auth(owner_token), json={}
    )
    assert published.status_code == 200, published.text
    return aid


_GOVERNED_KEYS = {
    "id",
    "name",
    "autonomyLevel",
    "owner",
    "status",
    "certificationState",
    "featured",
    "ownerOrphaned",
    "created_at",
    "updated_at",
}


# --- AC-1: an admin certifies / features / deprecates / disables -------------


async def test_admin_certifies_assistant_and_reads_back(
    client: AsyncClient, seeded: _Seeded
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    resp = await client.post(
        f"/api/v1/admin/assistants/{aid}/certify",
        headers=_auth(admin_token),
        json={"certificationState": "certified"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _GOVERNED_KEYS <= set(body)
    assert body["certificationState"] == "certified"

    # It reads back in the admin library list.
    listed = await client.get("/api/v1/admin/assistants", headers=_auth(admin_token))
    assert listed.status_code == 200, listed.text
    entry = next(a for a in listed.json()["items"] if a["id"] == aid)
    assert entry["certificationState"] == "certified"

    # And in the owner's own /assistants list (governance visible in list responses).
    owner_list = await client.get("/api/v1/assistants", headers=_auth(owner_token))
    owner_entry = next(a for a in owner_list.json()["items"] if a["id"] == aid)
    assert owner_entry["certificationState"] == "certified"


async def test_admin_deprecates_and_features_assistant(
    client: AsyncClient, seeded: _Seeded
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    dep = await client.post(
        f"/api/v1/admin/assistants/{aid}/certify",
        headers=_auth(admin_token),
        json={"certificationState": "deprecated"},
    )
    assert dep.status_code == 200, dep.text
    assert dep.json()["certificationState"] == "deprecated"

    feat = await client.post(
        f"/api/v1/admin/assistants/{aid}/feature",
        headers=_auth(admin_token),
        json={"featured": True},
    )
    assert feat.status_code == 200, feat.text
    assert feat.json()["featured"] is True


# --- AC-2: a disabled assistant cannot be started (negative, INV-8) ----------


async def test_disabled_assistant_cannot_start_chat(client: AsyncClient, seeded: _Seeded) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    # Before disable: the owner can start a chat from the published assistant.
    ok = await client.post(
        "/api/v1/chat/sessions", headers=_auth(owner_token), json={"assistant_id": aid}
    )
    assert ok.status_code == 201, ok.text

    disabled = await client.post(
        f"/api/v1/admin/assistants/{aid}/disable",
        headers=_auth(admin_token),
        json={"disabled": True},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["disabledAt"] is not None

    # After disable: starting a chat is refused (the "only a published assistant may
    # start" gate now sees status=disabled) — the mandatory negative.
    denied = await client.post(
        "/api/v1/chat/sessions", headers=_auth(owner_token), json={"assistant_id": aid}
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["code"] == "assistant_not_published"


async def test_disabled_assistant_cannot_be_scheduled(client: AsyncClient, seeded: _Seeded) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    await client.post(
        f"/api/v1/admin/assistants/{aid}/disable",
        headers=_auth(admin_token),
        json={"disabled": True},
    )
    # Scheduling a disabled assistant is rejected (a disabled assistant cannot run).
    sched = await client.post(
        "/api/v1/schedules",
        headers=_auth(owner_token),
        json={"assistant_id": aid, "cadence": {"cron": "0 8 * * *"}, "timezone": "UTC"},
    )
    assert sched.status_code == 422, sched.text
    assert sched.json()["code"] == "assistant_not_runnable"


async def test_reenable_returns_assistant_to_draft(client: AsyncClient, seeded: _Seeded) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    await client.post(
        f"/api/v1/admin/assistants/{aid}/disable",
        headers=_auth(admin_token),
        json={"disabled": True},
    )
    reenabled = await client.post(
        f"/api/v1/admin/assistants/{aid}/disable",
        headers=_auth(admin_token),
        json={"disabled": False},
    )
    assert reenabled.status_code == 200, reenabled.text
    # Re-enabling drops it back to draft (owner must re-publish before it runs again).
    assert reenabled.json()["status"] == "draft"
    assert reenabled.json()["disabledAt"] is None


# --- AC-3: orphaned detection + bulk disable/reassign (E6-8) -----------------


async def test_ownership_transfer_to_member(client: AsyncClient, seeded: _Seeded) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    resp = await client.post(
        f"/api/v1/admin/assistants/{aid}/transfer-ownership",
        headers=_auth(admin_token),
        json={"newOwner": str(seeded.member2_a)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["owner"] == str(seeded.member2_a)


async def test_ownership_transfer_to_non_member_is_422(
    client: AsyncClient, seeded: _Seeded
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    # A user from another tenant is not a member here → 422 (INV-8), never 404.
    resp = await client.post(
        f"/api/v1/admin/assistants/{aid}/transfer-ownership",
        headers=_auth(admin_token),
        json={"newOwner": str(seeded.admin_b)},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "owner_not_a_member"


async def test_orphaned_assistant_flagged_and_bulk_disabled(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    # Deprovision the owner (delete the user row) — the assistant is now orphaned.
    async with sessionmaker() as session:
        # Break the FK first (SQLite offline has no ON DELETE SET NULL), leaving a
        # dangling owner id so the "owner no longer a tenant member" check fires.
        await session.execute(
            update(models.Assistant)
            .where(models.Assistant.id == uuid.UUID(aid))
            .values(owner_id=uuid.uuid4())
        )
        await session.execute(delete(models.User).where(models.User.id == seeded.member_a))
        await session.commit()

    # The library flags it as orphaned.
    listed = await client.get("/api/v1/admin/assistants", headers=_auth(admin_token))
    entry = next(a for a in listed.json()["items"] if a["id"] == aid)
    assert entry["ownerOrphaned"] is True

    # Bulk-disable orphans disables exactly this assistant.
    swept = await client.post(
        "/api/v1/admin/assistants/disable-orphans", headers=_auth(admin_token)
    )
    assert swept.status_code == 200, swept.text
    assert aid in swept.json()["affected"]
    assert swept.json()["action"] == "disabled"

    # It is now disabled.
    listed2 = await client.get("/api/v1/admin/assistants", headers=_auth(admin_token))
    entry2 = next(a for a in listed2.json()["items"] if a["id"] == aid)
    assert entry2["status"] == "disabled"


# --- AC-N (negative): a non-admin governance mutation → 403 (INV-5) ----------


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("post", "/certify", {"certificationState": "certified"}),
        ("post", "/feature", {"featured": True}),
        ("post", "/disable", {"disabled": True}),
        ("post", "/transfer-ownership", {"newOwner": None}),
    ],
)
async def test_non_admin_governance_mutation_is_403(
    client: AsyncClient,
    seeded: _Seeded,
    method: str,
    suffix: str,
    payload: dict[str, object],
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    # The owner is a MEMBER, not an admin — a governance mutation is 403 (INV-5).
    if payload.get("newOwner") is None and suffix == "/transfer-ownership":
        payload = {"newOwner": str(seeded.member2_a)}
    resp = await client.request(
        method,
        f"/api/v1/admin/assistants/{aid}{suffix}",
        headers=_auth(owner_token),
        json=payload,
    )
    assert resp.status_code == 403, resp.text


async def test_non_admin_list_and_bulk_are_403(client: AsyncClient, seeded: _Seeded) -> None:
    member_token = await _login(client, seeded.member_a_email)
    for path in ("/api/v1/admin/assistants",):
        resp = await client.get(path, headers=_auth(member_token))
        assert resp.status_code == 403, resp.text
    swept = await client.post(
        "/api/v1/admin/assistants/disable-orphans", headers=_auth(member_token)
    )
    assert swept.status_code == 403, swept.text


async def test_governance_mutation_unauthenticated_is_401(
    client: AsyncClient,
    seeded: _Seeded,
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> None:
    resp = await client.post(
        f"/api/v1/admin/assistants/{uuid.uuid4()}/certify",
        json={"certificationState": "certified"},
    )
    assert resp.status_code == 401
    assert durable_audit_ledger.events == []


# --- INV-1: cross-tenant assistant id → 404 (existence non-disclosure) -------


async def test_cross_tenant_certify_is_404(
    client: AsyncClient,
    seeded: _Seeded,
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    # Tenant B's admin must not be able to govern tenant A's assistant → 404.
    admin_b_token = await _login(client, seeded.admin_b_email)
    resp = await client.post(
        f"/api/v1/admin/assistants/{aid}/certify",
        headers={**_auth(admin_b_token), "x-request-id": "req-governance-denied"},
        json={"certificationState": "certified"},
    )
    assert resp.status_code == 404, resp.text
    denied = [event for event in durable_audit_ledger.events if event.resource_id == aid]
    assert len(denied) == 1
    assert denied[0].actor_id == seeded.admin_b
    assert denied[0].tenant_id == seeded.tenant_b
    assert denied[0].request_id == "req-governance-denied"
    assert denied[0].metadata == {
        "attempted_action": "assistant.certify",
        "reason": "not_visible",
    }

    # And it never appears in tenant B's library (INV-1).
    listed_b = await client.get("/api/v1/admin/assistants", headers=_auth(admin_b_token))
    assert all(a["id"] != aid for a in listed_b.json()["items"])


async def test_unknown_governance_ids_emit_one_action_specific_denial_each(
    client: AsyncClient,
    seeded: _Seeded,
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> None:
    """Every direct-id governance mutation owns one non-enumerating denial."""
    admin_token = await _login(client, seeded.admin_a_email)
    cases = (
        ("certify", {"certificationState": "certified"}, "assistant.certify"),
        ("feature", {"featured": True}, "assistant.feature"),
        ("disable", {"disabled": True}, "assistant.disable"),
        (
            "transfer-ownership",
            {"newOwner": str(seeded.member2_a)},
            "assistant.transfer_ownership",
        ),
    )
    expected: dict[str, tuple[str, str]] = {}
    for ordinal, (suffix, payload, attempted_action) in enumerate(cases):
        assistant_id = str(uuid.uuid4())
        request_id = f"req-governance-unknown-{ordinal}"
        response = await client.post(
            f"/api/v1/admin/assistants/{assistant_id}/{suffix}",
            headers={**_auth(admin_token), "x-request-id": request_id},
            json=payload,
        )
        assert response.status_code == 404
        expected[request_id] = (assistant_id, attempted_action)

    denied = [event for event in durable_audit_ledger.events if event.request_id in expected]
    assert len(denied) == len(cases)
    for event in denied:
        assistant_id, attempted_action = expected[event.request_id]
        assert event.resource_id == assistant_id
        assert event.tenant_id == seeded.tenant_a
        assert event.actor_id == seeded.admin_a
        assert event.metadata == {
            "attempted_action": attempted_action,
            "reason": "not_visible",
        }


async def test_governance_denial_propagates_audit_failure(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    durable_audit_ledger: RecordingDurableAuditTransactions,
) -> None:
    durable_audit_ledger.fail_with = RuntimeError("audit unavailable")
    async with sessionmaker() as session:
        service = AssistantGovernanceService(
            session,
            tenant_id=seeded.tenant_a,
            actor_id=seeded.admin_a,
            denials=denial_recorder(durable_audit_ledger, session, seeded.tenant_a),
            request_id="req-governance-audit-failure",
            source_ip="203.0.113.10",
        )
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await service.certify(uuid.uuid4(), state=CertificationState.CERTIFIED)
    assert durable_audit_ledger.events == []


# --- INV-6: governance mutations are audited --------------------------------


async def test_certify_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    await client.post(
        f"/api/v1/admin/assistants/{aid}/certify",
        headers=_auth(admin_token),
        json={"certificationState": "certified"},
    )
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    certified = [e for e in events if e.action == "assistant.certified"]
    assert len(certified) == 1
    ev = certified[0]
    assert ev.resource_type == "assistant"
    assert ev.resource_id == aid
    assert ev.metadata["certification_state"] == "certified"


async def test_ownership_transfer_is_audited(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    await client.post(
        f"/api/v1/admin/assistants/{aid}/transfer-ownership",
        headers=_auth(admin_token),
        json={"newOwner": str(seeded.member2_a)},
    )
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    transferred = [e for e in events if e.action == "assistant.ownership_transferred"]
    assert len(transferred) == 1
    ev = transferred[0]
    assert ev.metadata["previous_owner_id"] == str(seeded.member_a)
    assert ev.metadata["new_owner_id"] == str(seeded.member2_a)


async def test_disable_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    owner_token = await _login(client, seeded.member_a_email)
    aid = await _create_published_assistant(client, owner_token, backup_owner=seeded.member2_a)
    admin_token = await _login(client, seeded.admin_a_email)

    await client.post(
        f"/api/v1/admin/assistants/{aid}/disable",
        headers=_auth(admin_token),
        json={"disabled": True},
    )
    async with sessionmaker() as session:
        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
    disabled = [e for e in events if e.action == "assistant.disabled"]
    assert len(disabled) == 1
    assert disabled[0].metadata["disabled"] is True
