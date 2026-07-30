"""Groups admin API tests — ``/admin/groups`` end-to-end (ADR-0022 §2).

Drives the real FastAPI app against an **offline** in-memory SQLite database,
mirroring ``test_admin_api``: ``get_db_session`` is overridden to yield sessions
from a StaticPool engine whose schema is built from the ORM metadata, and two
tenants with a mix of roles are seeded so the role and tenant negatives are real.
Authentication is a real login — ``current_user``/``require_roles`` are never
overridden, because they are precisely what these negatives exist to prove.

The contract (``contracts/openapi.yaml`` §``/admin/groups``) and the guarantees:

* INV-5: a non-admin (member / security) on **every** groups path → **403**;
* INV-4: a missing bearer → **401**;
* INV-1: a group — or a user — from another tenant → **404**, never 403;
* the derived "All members" group is listed but every mutation on it → **409**;
* duplicate name (case-insensitive) → **409**; blank / over-long name → **422**;
* membership add/remove are idempotent and land as 204.
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
from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    GroupRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import (
    DocumentStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    Role,
)
from app.domain.llm import Embedding
from app.main import create_app
from app.retrieval import RetrievalService

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PASSWORD = "devpassword"


class _FakeGateway:
    """Deterministic embedder — the retrieval assertions here are relational."""

    async def embed(self, texts: list[str]) -> list[Embedding]:
        return [Embedding(values=[0.1] * 1024) for _ in texts]


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
        member_a_id: uuid.UUID,
        member_b_id: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.admin_a_email = admin_a_email
        self.member_a_email = member_a_email
        self.security_a_email = security_a_email
        self.admin_b_email = admin_b_email
        self.member_a_id = member_a_id
        self.member_b_id = member_b_id


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
            member_a = await UserRepository(seed, tenant_a.id).create(
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
            member_b = await UserRepository(seed, tenant_b.id).create(
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
            security_a_email="security@acme.test",
            admin_b_email="admin@globex.test",
            member_a_id=member_a.id,
            member_b_id=member_b.id,
        )
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> Iterator[FastAPI]:
    """The app with its DB session pointed at the offline SQLite engine."""
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


async def _create_group(client: AsyncClient, token: str, name: str) -> dict[str, object]:
    resp = await client.post("/api/v1/admin/groups", json={"name": name}, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


# --- happy path -------------------------------------------------------------


async def test_admin_creates_lists_renames_and_deletes_a_group(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)

    created = await _create_group(client, token, "Tax Team")
    assert created["name"] == "Tax Team"
    assert created["kind"] == "user"
    assert created["member_count"] == 0

    listed = await client.get("/api/v1/admin/groups", headers=_auth(token))
    assert listed.status_code == 200, listed.text
    # The derived "All members" group is materialized by the first listing and
    # sorts first; the created group follows.
    assert [g["name"] for g in listed.json()["items"]] == ["All members", "Tax Team"]

    group_id = created["id"]
    renamed = await client.patch(
        f"/api/v1/admin/groups/{group_id}", json={"name": "Tax & Audit"}, headers=_auth(token)
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Tax & Audit"

    fetched = await client.get(f"/api/v1/admin/groups/{group_id}", headers=_auth(token))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["name"] == "Tax & Audit"

    deleted = await client.delete(f"/api/v1/admin/groups/{group_id}", headers=_auth(token))
    assert deleted.status_code == 204, deleted.text
    gone = await client.get(f"/api/v1/admin/groups/{group_id}", headers=_auth(token))
    assert gone.status_code == 404, gone.text


async def test_membership_round_trip_is_idempotent(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.admin_a_email)
    group_id = (await _create_group(client, token, "Tax Team"))["id"]
    body = {"user_id": str(seeded.member_a_id)}

    first = await client.post(
        f"/api/v1/admin/groups/{group_id}/members", json=body, headers=_auth(token)
    )
    assert first.status_code == 204, first.text
    again = await client.post(
        f"/api/v1/admin/groups/{group_id}/members", json=body, headers=_auth(token)
    )
    assert again.status_code == 204, again.text  # idempotent, not 409

    members = await client.get(f"/api/v1/admin/groups/{group_id}/members", headers=_auth(token))
    assert members.status_code == 200, members.text
    assert [m["email"] for m in members.json()["items"]] == [seeded.member_a_email]

    group = await client.get(f"/api/v1/admin/groups/{group_id}", headers=_auth(token))
    assert group.json()["member_count"] == 1

    removed = await client.delete(
        f"/api/v1/admin/groups/{group_id}/members/{seeded.member_a_id}",
        headers=_auth(token),
    )
    assert removed.status_code == 204, removed.text
    removed_again = await client.delete(
        f"/api/v1/admin/groups/{group_id}/members/{seeded.member_a_id}",
        headers=_auth(token),
    )
    assert removed_again.status_code == 204, removed_again.text  # idempotent

    empty = await client.get(f"/api/v1/admin/groups/{group_id}/members", headers=_auth(token))
    assert empty.json()["items"] == []


# --- INV-5: role gating -----------------------------------------------------


@pytest.mark.parametrize("actor", ["member", "security"])
async def test_non_admin_is_forbidden_on_every_groups_path(
    client: AsyncClient, seeded: _Seeded, actor: str
) -> None:
    """INV-5: only an admin manages groups — every path, every method → 403."""
    admin = await _login(client, seeded.admin_a_email)
    group_id = (await _create_group(client, admin, "Tax Team"))["id"]

    email = seeded.member_a_email if actor == "member" else seeded.security_a_email
    token = await _login(client, email)
    member_path = f"/api/v1/admin/groups/{group_id}/members"
    calls = (
        client.get("/api/v1/admin/groups", headers=_auth(token)),
        client.post("/api/v1/admin/groups", json={"name": "X"}, headers=_auth(token)),
        client.get(f"/api/v1/admin/groups/{group_id}", headers=_auth(token)),
        client.patch(f"/api/v1/admin/groups/{group_id}", json={"name": "X"}, headers=_auth(token)),
        client.delete(f"/api/v1/admin/groups/{group_id}", headers=_auth(token)),
        client.get(member_path, headers=_auth(token)),
        client.post(member_path, json={"user_id": str(seeded.member_a_id)}, headers=_auth(token)),
        client.delete(f"{member_path}/{seeded.member_a_id}", headers=_auth(token)),
    )
    for call in calls:
        resp = await call
        assert resp.status_code == 403, resp.text
        assert resp.headers["content-type"].startswith("application/problem+json")


async def test_missing_bearer_is_401(client: AsyncClient) -> None:
    """INV-4: no token → 401, before any role check."""
    resp = await client.get("/api/v1/admin/groups")
    assert resp.status_code == 401, resp.text


# --- INV-1: tenant scoping --------------------------------------------------


async def test_group_from_another_tenant_is_404_not_403(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """A tenant-B group is invisible to a tenant-A admin — 404, never 403."""
    token_b = await _login(client, seeded.admin_b_email)
    group_b = (await _create_group(client, token_b, "Globex Tax"))["id"]

    token_a = await _login(client, seeded.admin_a_email)
    for resp in (
        await client.get(f"/api/v1/admin/groups/{group_b}", headers=_auth(token_a)),
        await client.patch(
            f"/api/v1/admin/groups/{group_b}", json={"name": "X"}, headers=_auth(token_a)
        ),
        await client.delete(f"/api/v1/admin/groups/{group_b}", headers=_auth(token_a)),
        await client.get(f"/api/v1/admin/groups/{group_b}/members", headers=_auth(token_a)),
    ):
        assert resp.status_code == 404, resp.text

    # ...and tenant A's list never leaks it.
    listed = await client.get("/api/v1/admin/groups", headers=_auth(token_a))
    assert group_b not in {g["id"] for g in listed.json()["items"]}


async def test_adding_a_user_from_another_tenant_is_404(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)
    group_id = (await _create_group(client, token, "Tax Team"))["id"]
    resp = await client.post(
        f"/api/v1/admin/groups/{group_id}/members",
        json={"user_id": str(seeded.member_b_id)},
        headers=_auth(token),
    )
    assert resp.status_code == 404, resp.text


# --- conflicts and validation ----------------------------------------------


async def test_duplicate_name_is_409_case_insensitively(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.admin_a_email)
    await _create_group(client, token, "Tax Team")
    resp = await client.post(
        "/api/v1/admin/groups", json={"name": "tax team"}, headers=_auth(token)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "group_name_taken"


@pytest.mark.parametrize("name", ["", "   ", "x" * 121])
async def test_invalid_name_is_422(client: AsyncClient, seeded: _Seeded, name: str) -> None:
    """Blank, whitespace-only and over-long names are all rejected (INV-8)."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.post("/api/v1/admin/groups", json={"name": name}, headers=_auth(token))
    assert resp.status_code == 422, resp.text


async def test_unknown_field_is_rejected(client: AsyncClient, seeded: _Seeded) -> None:
    """The wire models are closed (``extra: forbid``) — a stray field is 422."""
    token = await _login(client, seeded.admin_a_email)
    resp = await client.post(
        "/api/v1/admin/groups",
        json={"name": "Tax Team", "kind": "system"},
        headers=_auth(token),
    )
    assert resp.status_code == 422, resp.text


# --- the derived "All members" group ---------------------------------------


async def test_system_group_is_created_by_the_api_listed_and_immutable(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """ADR-0022 §3: created lazily by production code, listed first, immutable.

    Deliberately does NOT seed the group through the repository — that would
    mask the fact that nothing in production creates it. The only way it can
    appear here is the listing route materializing it.
    """
    token = await _login(client, seeded.admin_a_email)
    await _create_group(client, token, "Tax Team")

    listed = await client.get("/api/v1/admin/groups", headers=_auth(token))
    items = listed.json()["items"]
    assert items[0]["kind"] == "system", "system group is listed first"
    assert items[0]["name"] == "All members"
    # null, not 0 — "everyone", not "empty". The key must be present.
    assert "member_count" in items[0]
    assert items[0]["member_count"] is None
    # ...and the user group's count is a real integer, not null.
    assert items[1]["member_count"] == 0

    sid = uuid.UUID(str(items[0]["id"]))
    member_path = f"/api/v1/admin/groups/{sid}/members"
    for resp in (
        await client.patch(
            f"/api/v1/admin/groups/{sid}", json={"name": "Everyone"}, headers=_auth(token)
        ),
        await client.delete(f"/api/v1/admin/groups/{sid}", headers=_auth(token)),
        await client.post(
            member_path, json={"user_id": str(seeded.member_a_id)}, headers=_auth(token)
        ),
        await client.delete(f"{member_path}/{seeded.member_a_id}", headers=_auth(token)),
    ):
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "system_group_immutable"

    # It is still readable, and its member list is empty (derived, not stored).
    members = await client.get(member_path, headers=_auth(token))
    assert members.status_code == 200, members.text
    assert members.json()["items"] == []


# --- the API actually moves the permission needle ---------------------------


async def test_adding_a_member_via_the_api_widens_retrieval_and_removing_revokes(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """End-to-end tie between this API and the INV-2 widening (ADR-0022 §5).

    The unit tests prove the chokepoint honours group grants and the route tests
    prove the API manages membership; nothing yet proves the two are wired to
    each other. This does: an admin creates a group and adds a member **over
    HTTP**, and a document granted to that group becomes retrievable by the
    member — then stops when the admin removes them, on the very next request.
    """
    token = await _login(client, seeded.admin_a_email)
    group_id = (await _create_group(client, token, "Tax Team"))["id"]

    # A document owned by the admin, shared with the group by a collection grant.
    async with sessionmaker() as session:
        admin_user = await UserRepository(session, seeded.tenant_a).get_by_email(
            seeded.admin_a_email
        )
        assert admin_user is not None
        collection = await CollectionRepository(session, seeded.tenant_a).create(
            owner_id=admin_user.id, name="Tax pack"
        )
        document = await DocumentRepository(session, seeded.tenant_a).create(
            owner_id=admin_user.id,
            collection_id=collection.id,
            filename="ny-it201.txt",
            mime_type="text/plain",
            size_bytes=10,
            storage_key="k",
            acl_enforced=False,
            status=DocumentStatus.READY,
        )
        await ChunkRepository(session, seeded.tenant_a).replace_for_document(
            document.id, [ChunkInput(text="body", char_start=0, char_end=4)]
        )
        await GrantRepository(session, seeded.tenant_a).create(
            resource_type=GrantResourceType.COLLECTION,
            resource_id=collection.id,
            principal_type=GrantPrincipalType.GROUP,
            principal_id=uuid.UUID(str(group_id)),
            role=GrantRole.VIEWER,
            granted_by=admin_user.id,
        )
        await session.commit()

    member_principal = Principal(
        user_id=seeded.member_a_id, tenant_id=seeded.tenant_a, roles=(Role.MEMBER,)
    )

    async def _visible_to_member() -> set[uuid.UUID]:
        # A fresh session + service models the member's NEXT request, so nothing
        # is carried over from the admin's calls.
        async with sessionmaker() as session:
            matches = await RetrievalService(session, gateway=_FakeGateway()).list_documents(
                principal=member_principal
            )
            return {m.document_id for m in matches}

    assert document.id not in await _visible_to_member(), "not a member yet"

    added = await client.post(
        f"/api/v1/admin/groups/{group_id}/members",
        json={"user_id": str(seeded.member_a_id)},
        headers=_auth(token),
    )
    assert added.status_code == 204, added.text
    assert document.id in await _visible_to_member(), "membership must widen retrieval"

    removed = await client.delete(
        f"/api/v1/admin/groups/{group_id}/members/{seeded.member_a_id}",
        headers=_auth(token),
    )
    assert removed.status_code == 204, removed.text
    assert document.id not in await _visible_to_member(), "removal must revoke at once"


async def test_reserved_system_group_name_cannot_be_squatted(
    client: AsyncClient, seeded: _Seeded
) -> None:
    """A user group must not occupy the derived group's name (ADR-0022 §3).

    Names are unique per tenant case-insensitively and the system group is
    created lazily, so squatting "All members" would block it **permanently**
    for that tenant. Refused up front, on create and on rename.
    """
    token = await _login(client, seeded.admin_a_email)
    squat = await client.post(
        "/api/v1/admin/groups", json={"name": "all MEMBERS"}, headers=_auth(token)
    )
    assert squat.status_code == 409, squat.text
    assert squat.json()["code"] == "group_name_reserved"

    group_id = (await _create_group(client, token, "Tax Team"))["id"]
    rename = await client.patch(
        f"/api/v1/admin/groups/{group_id}",
        json={"name": "All members"},
        headers=_auth(token),
    )
    assert rename.status_code == 409, rename.text
    assert rename.json()["code"] == "group_name_reserved"


async def test_cross_tenant_membership_writes_are_404(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-1 on the two routes that WRITE: neither may cross a tenant boundary."""
    token_b = await _login(client, seeded.admin_b_email)
    group_b = (await _create_group(client, token_b, "Globex Tax"))["id"]

    token_a = await _login(client, seeded.admin_a_email)
    add = await client.post(
        f"/api/v1/admin/groups/{group_b}/members",
        json={"user_id": str(seeded.member_a_id)},
        headers=_auth(token_a),
    )
    assert add.status_code == 404, add.text
    remove = await client.delete(
        f"/api/v1/admin/groups/{group_b}/members/{seeded.member_a_id}",
        headers=_auth(token_a),
    )
    assert remove.status_code == 404, remove.text

    # The foreign group's membership is unchanged.
    members_b = await client.get(f"/api/v1/admin/groups/{group_b}/members", headers=_auth(token_b))
    assert members_b.json()["items"] == []


async def test_route_mutations_are_persisted_and_audited(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """INV-6 at the ROUTE seam: the change and its audit event both commit.

    The service unit tests prove the sink is called; only this proves the route
    commits the transaction the audit write shares — a missing ``commit()``
    would roll both back and still return 2xx.
    """
    token = await _login(client, seeded.admin_a_email)
    group_id = (await _create_group(client, token, "Tax Team"))["id"]
    added = await client.post(
        f"/api/v1/admin/groups/{group_id}/members",
        json={"user_id": str(seeded.member_a_id)},
        headers=_auth(token),
    )
    assert added.status_code == 204, added.text

    # A brand-new session sees committed rows only.
    async with sessionmaker() as session:
        group = await GroupRepository(session, seeded.tenant_a).get(uuid.UUID(str(group_id)))
        assert group is not None, "the created group must survive the request"
        member_ids = await GroupRepository(session, seeded.tenant_a).list_member_ids(
            uuid.UUID(str(group_id))
        )
        assert member_ids == [seeded.member_a_id], "membership must be committed"
        actions = {
            e.action
            for e in await AuditEventRepository(session, seeded.tenant_a).list_recent(limit=20)
        }
    assert AuditAction.GROUP_CREATED.value in actions
    assert AuditAction.GROUP_MEMBER_ADDED.value in actions
