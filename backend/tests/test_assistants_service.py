"""Assistant core service — CRUD + publish/version/rollback + negatives (issue #211).

Drives :class:`~app.services.assistants_service.AssistantsService` end-to-end on
offline in-memory SQLite (the ``assistants`` / ``assistant_versions`` tables are
plain relational SQL, so the whole path runs without Postgres). These pin the
acceptance criteria and invariants ADR-0011 makes non-negotiable:

* **AC-1** — create → get → list → update → delete; publish blocked without a
  distinct backup owner → **422** (the frozen contract's chosen code for the
  illegal transition, INV-8).
* **AC-4** — a ``tool_allowlist`` entry not in the CC-A registry → **422** (deny
  by default; an assistant can never reference an unknown tool).
* **AC-5** — a cross-tenant / non-owned assistant id → **404** (INV-1/INV-2,
  existence non-disclosure, never 403); every mutation is audited (INV-6).
* Versioning + rollback: publish freezes an immutable version; rollback appends a
  **new** version from a prior snapshot (history is never mutated).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import NotFoundError, ValidationError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    CollectionRepository,
    GrantRepository,
    McpServerRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    KnowledgeScope,
    McpServerStatus,
    Role,
)
from app.services.assistants_service import AssistantsService
from app.services.audit import AuditSink
from app.services.tools.mcp_bridge import namespaced_tool_name, slug_for_server

import app.db.models  # noqa: F401  isort: skip


# ---------------------------------------------------------------------------
# Offline fixtures: in-memory SQLite schema + two tenants (mirrors test_secrets).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A fresh in-memory SQLite schema + session per test (offline-safe)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


class _World:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice: uuid.UUID,
        bob: uuid.UUID,
        carol: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice  # owner in tenant A
        self.bob = bob  # backup / other owner in tenant A
        self.carol = carol  # a user in tenant B


@pytest_asyncio.fixture
async def world(session: AsyncSession) -> _World:
    tenants = TenantRepository(session)
    ta = await tenants.create(name="Acme")
    tb = await tenants.create(name="Globex")
    alice = await UserRepository(session, ta.id).create(
        email="alice@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    bob = await UserRepository(session, ta.id).create(
        email="bob@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    carol = await UserRepository(session, tb.id).create(
        email="carol@globex.test", password_hash="h", roles=[Role.MEMBER]
    )
    await session.commit()
    return _World(tenant_a=ta.id, tenant_b=tb.id, alice=alice.id, bob=bob.id, carol=carol.id)


def _service(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    roles: tuple[Role, ...] = (Role.MEMBER,),
) -> AssistantsService:
    audit = AuditSink(AuditEventRepository(session, tenant_id))
    return AssistantsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        audit=audit,
        request_id="req-test",
        source_ip="203.0.113.1",
    )


async def _audit_actions(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    events = await AuditEventRepository(session, tenant_id).list_recent(limit=50)
    return [e.action for e in events]


# ---------------------------------------------------------------------------
# AC-1: CRUD happy path.
# ---------------------------------------------------------------------------


async def test_create_get_list_update_delete(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)

    created = await svc.create(name="Renewals", instructions="Be terse.")
    assert created.status is AssistantStatus.DRAFT
    assert created.owner_id == world.alice
    assert created.current_version is None

    got = await svc.get(created.id)
    assert got.id == created.id
    assert got.name == "Renewals"

    page = await svc.list_(cursor=None, limit=10)
    assert [a.id for a in page.items] == [created.id]

    updated = await svc.update(created.id, name="Renewals v2", instructions="Be terse and cite.")
    assert updated.name == "Renewals v2"
    assert updated.instructions == "Be terse and cite."

    await svc.delete(created.id)
    with pytest.raises(NotFoundError):
        await svc.get(created.id)


async def test_create_rejects_blank_name(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    with pytest.raises(ValidationError):
        await svc.create(name="   ")


# ---------------------------------------------------------------------------
# AC-1: publish requires a distinct backup owner → 422 without it.
# ---------------------------------------------------------------------------


async def test_publish_blocked_without_backup_owner(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(name="No backup")
    with pytest.raises(ValidationError) as exc:
        await svc.publish(assistant.id)
    assert exc.value.status == 422
    assert exc.value.code == "backup_owner_required"


async def test_publish_blocked_when_backup_equals_owner(
    session: AsyncSession, world: _World
) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    # The service create validates distinctness up front, so a same-as-owner backup
    # is rejected at create time — assert that 422 as the equivalent negative.
    with pytest.raises(ValidationError) as exc:
        await svc.create(name="Self backup", backup_owner_id=world.alice)
    assert exc.value.code == "backup_owner_same_as_owner"


async def test_publish_with_backup_owner_creates_version(
    session: AsyncSession, world: _World
) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(name="Publishable", backup_owner_id=world.bob)
    version = await svc.publish(assistant.id, notes="v1")
    assert version.version == 1
    assert version.notes == "v1"
    assert version.config["name"] == "Publishable"

    after = await svc.get(assistant.id)
    assert after.status is AssistantStatus.PUBLISHED
    assert after.current_version == 1


# ---------------------------------------------------------------------------
# AC-4: an unknown tool in the allow-list → 422 (deny by default).
# ---------------------------------------------------------------------------


async def test_unknown_tool_in_allowlist_is_422(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    with pytest.raises(ValidationError) as exc:
        await svc.create(name="Bad tools", tool_allowlist=("nonexistent_tool",))
    assert exc.value.status == 422
    assert exc.value.code == "unknown_tool"


async def test_known_tool_in_allowlist_is_accepted(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    # search_text is a real registered retrieval tool (CC-A #207).
    assistant = await svc.create(name="Good tools", tool_allowlist=("search_text",))
    assert assistant.tool_allowlist == ("search_text",)


async def test_update_rejects_unknown_tool(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(name="Editable")
    with pytest.raises(ValidationError) as exc:
        await svc.update(assistant.id, tool_allowlist=["made_up"])
    assert exc.value.code == "unknown_tool"


# ---------------------------------------------------------------------------
# #227: an assistant may allow-list its own registered MCP tools; a bogus /
# cross-tenant ``mcp:*`` name is rejected (deny by default, INV-1/INV-2).
# ---------------------------------------------------------------------------


async def _register_mcp_server(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    tool_name: str = "echo",
) -> str:
    """Register an MCP server with one discovered tool; return its namespaced name."""
    repo = McpServerRepository(session, tenant_id)
    server = await repo.create(
        owner_id=owner_id,
        name="fixture",
        transport="streamable_http",
        endpoint_url="https://mcp.example.com/mcp",
        auth_secret_ref=None,
        secret_hint=None,
    )
    await repo.update_health(
        server.id,
        status=McpServerStatus.READY,
        last_health_at=None,
        last_error=None,
        discovered_tools=[
            {"name": tool_name, "description": "d", "input_schema": {}, "read_only": True}
        ],
    )
    await session.commit()
    return namespaced_tool_name(slug_for_server(server), tool_name)


async def test_assistant_may_allowlist_its_own_mcp_tool(
    session: AsyncSession, world: _World
) -> None:
    tool_name = await _register_mcp_server(
        session, tenant_id=world.tenant_a, owner_id=world.alice
    )
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(name="MCP-enabled", tool_allowlist=(tool_name,))
    assert assistant.tool_allowlist == (tool_name,)


async def test_bogus_mcp_tool_name_is_rejected(session: AsyncSession, world: _World) -> None:
    # A well-formed but non-existent ``mcp:*`` id (no such server/tool for the caller).
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    with pytest.raises(ValidationError) as exc:
        await svc.create(name="Bad mcp", tool_allowlist=("mcp:srv-deadbeef0000:ghost",))
    assert exc.value.code == "unknown_tool"


async def test_cross_owner_mcp_tool_name_is_rejected(
    session: AsyncSession, world: _World
) -> None:
    # Bob (same tenant) registers a server; Alice cannot name Bob's tool (INV-2).
    bobs_tool = await _register_mcp_server(
        session, tenant_id=world.tenant_a, owner_id=world.bob
    )
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    with pytest.raises(ValidationError) as exc:
        await svc.create(name="Steal Bob's tool", tool_allowlist=(bobs_tool,))
    assert exc.value.code == "unknown_tool"


async def test_cross_tenant_mcp_tool_name_is_rejected(
    session: AsyncSession, world: _World
) -> None:
    # Carol (tenant B) registers a server; Alice (tenant A) cannot name it (INV-1).
    carols_tool = await _register_mcp_server(
        session, tenant_id=world.tenant_b, owner_id=world.carol
    )
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    with pytest.raises(ValidationError) as exc:
        await svc.create(name="Cross-tenant mcp", tool_allowlist=(carols_tool,))
    assert exc.value.code == "unknown_tool"


# ---------------------------------------------------------------------------
# Knowledge scope: an unowned collection/source id → 422.
# ---------------------------------------------------------------------------


async def test_scope_unowned_collection_is_422(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    # A collection owned by bob (a different user in the same tenant): alice cannot
    # scope to it (she neither owns nor is granted it).
    bob_coll = await CollectionRepository(session, world.tenant_a).create(
        owner_id=world.bob, name="bob-only"
    )
    await session.commit()
    scope = KnowledgeScope(collection_ids=(bob_coll.id,))
    with pytest.raises(ValidationError) as exc:
        await svc.create(name="Scoped", knowledge_scope=scope)
    assert exc.value.code == "scope_collection_forbidden"


async def test_scope_owned_collection_is_accepted(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    alice_coll = await CollectionRepository(session, world.tenant_a).create(
        owner_id=world.alice, name="alice-a"
    )
    await session.commit()
    scope = KnowledgeScope(collection_ids=(alice_coll.id,))
    assistant = await svc.create(name="Scoped", knowledge_scope=scope)
    assert assistant.knowledge_scope.collection_ids == (alice_coll.id,)


async def test_scope_granted_collection_is_accepted(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    bob_coll = await CollectionRepository(session, world.tenant_a).create(
        owner_id=world.bob, name="bob-shared"
    )
    # Grant alice viewer access to bob's collection (the existing grants pattern).
    await GrantRepository(session, world.tenant_a).create(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=bob_coll.id,
        principal_type=GrantPrincipalType.USER,
        principal_id=world.alice,
        role=GrantRole.VIEWER,
        granted_by=world.bob,
    )
    await session.commit()
    scope = KnowledgeScope(collection_ids=(bob_coll.id,))
    assistant = await svc.create(name="Granted scope", knowledge_scope=scope)
    assert assistant.knowledge_scope.collection_ids == (bob_coll.id,)


async def test_scope_unowned_source_is_422(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    bob_source = await SourceRepository(session, world.tenant_a).create(
        owner_id=world.bob, type="web", config={"url": "https://x"}
    )
    await session.commit()
    scope = KnowledgeScope(source_ids=(bob_source.id,))
    with pytest.raises(ValidationError) as exc:
        await svc.create(name="Src", knowledge_scope=scope)
    assert exc.value.code == "scope_source_forbidden"


# ---------------------------------------------------------------------------
# AC-5 / INV-1 / INV-2: cross-tenant / non-owned assistant id → 404.
# ---------------------------------------------------------------------------


async def test_cross_tenant_get_is_404(session: AsyncSession, world: _World) -> None:
    owner_svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await owner_svc.create(name="A-only")
    await session.commit()
    # Carol in tenant B cannot see it — 404, never a leak that it exists.
    carol_svc = _service(session, tenant_id=world.tenant_b, owner_id=world.carol)
    with pytest.raises(NotFoundError):
        await carol_svc.get(assistant.id)


async def test_non_owner_same_tenant_get_is_404(session: AsyncSession, world: _World) -> None:
    alice_svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await alice_svc.create(name="Alice's")
    await session.commit()
    # Bob (same tenant, not owner, not admin) gets 404 — indistinguishable from
    # non-existent (existence non-disclosure, INV-2).
    bob_svc = _service(session, tenant_id=world.tenant_a, owner_id=world.bob)
    with pytest.raises(NotFoundError):
        await bob_svc.get(assistant.id)
    with pytest.raises(NotFoundError):
        await bob_svc.update(assistant.id, name="hijack")
    with pytest.raises(NotFoundError):
        await bob_svc.delete(assistant.id)


async def test_tenant_admin_may_manage_others(session: AsyncSession, world: _World) -> None:
    alice_svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await alice_svc.create(name="Alice's")
    await session.commit()
    # An admin in the same tenant may manage it (owner-or-admin rule).
    admin_svc = _service(
        session, tenant_id=world.tenant_a, owner_id=world.bob, roles=(Role.ADMIN,)
    )
    got = await admin_svc.get(assistant.id)
    assert got.id == assistant.id


# ---------------------------------------------------------------------------
# INV-6: every mutation is audited.
# ---------------------------------------------------------------------------


async def test_mutations_are_audited(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(name="Audited", backup_owner_id=world.bob)
    await svc.update(assistant.id, name="Audited v2")
    await svc.publish(assistant.id)
    await svc.rollback(assistant.id, version=1)
    await svc.delete(assistant.id)
    actions = await _audit_actions(session, world.tenant_a)
    assert "assistant.created" in actions
    assert "assistant.updated" in actions
    assert "assistant.published" in actions
    assert "assistant.rolled_back" in actions
    assert "assistant.deleted" in actions


# ---------------------------------------------------------------------------
# Versioning + rollback: rollback appends a new version, never mutates history.
# ---------------------------------------------------------------------------


async def test_rollback_appends_new_version_from_prior_snapshot(
    session: AsyncSession, world: _World
) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(
        name="Evolving", instructions="v1 instructions", backup_owner_id=world.bob
    )
    v1 = await svc.publish(assistant.id)
    assert v1.version == 1

    # Edit the head + publish a second version.
    await svc.update(assistant.id, instructions="v2 instructions")
    v2 = await svc.publish(assistant.id)
    assert v2.version == 2
    assert v2.config["instructions"] == "v2 instructions"

    # Roll back to v1: a NEW version (v3) is appended whose config copies v1's.
    v3 = await svc.rollback(assistant.id, version=1)
    assert v3.version == 3
    assert v3.config["instructions"] == "v1 instructions"

    # History is intact: v1 and v2 still exist unchanged, plus the new v3.
    page = await svc.list_versions(assistant.id, cursor=None, limit=10)
    versions = {v.version: v for v in page.items}
    assert set(versions) == {1, 2, 3}
    assert versions[1].config["instructions"] == "v1 instructions"
    assert versions[2].config["instructions"] == "v2 instructions"

    # The head now runs the rolled-back config.
    head = await svc.get(assistant.id)
    assert head.instructions == "v1 instructions"


async def test_rollback_unknown_version_is_422(session: AsyncSession, world: _World) -> None:
    svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(name="One version", backup_owner_id=world.bob)
    await svc.publish(assistant.id)
    with pytest.raises(ValidationError) as exc:
        await svc.rollback(assistant.id, version=99)
    assert exc.value.code == "unknown_version"


async def test_list_versions_cross_tenant_is_404(session: AsyncSession, world: _World) -> None:
    alice_svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await alice_svc.create(name="A", backup_owner_id=world.bob)
    await alice_svc.publish(assistant.id)
    await session.commit()
    carol_svc = _service(session, tenant_id=world.tenant_b, owner_id=world.carol)
    with pytest.raises(NotFoundError):
        await carol_svc.list_versions(assistant.id, cursor=None, limit=10)
