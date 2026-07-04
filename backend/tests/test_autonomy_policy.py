"""Autonomy caps + enforcement — the #218 governance seam (negative-first).

Covers the per-tenant autonomy cap end-to-end on offline in-memory SQLite (the
``tenant_autonomy_policy`` / ``assistants`` tables are plain relational SQL, so the
whole path runs without Postgres):

* the enum ordering + clamp (``min(assistant, cap)``) that makes a cap a ceiling;
* :class:`~app.services.autonomy_policy_service.AutonomyPolicyService` read/write +
  the ``AutonomyPolicyReader.clamp`` enforcement helper;
* **AC-2 (negative):** publishing an assistant ABOVE the tenant cap is rejected 422
  (INV-8 illegal transition) — proved by lowering the cap under an act_auto assistant;
* **AC-3:** the effective autonomy (``min(configured, cap)``) is visible on the
  assistant projection the library/run detail reads;
* **INV-1:** a cap set in one tenant never lowers another tenant's effective autonomy.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import ValidationError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    TenantAutonomyPolicyRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import AutonomyLevel, Role
from app.services.assistants_service import AssistantsService
from app.services.audit import AuditSink
from app.services.autonomy_policy_service import (
    AutonomyPolicyReader,
    AutonomyPolicyService,
)

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


# --- enum ordering / clamp (pure) ------------------------------------------


def test_autonomy_rank_is_totally_ordered() -> None:
    assert AutonomyLevel.SUGGEST.rank == 0
    assert AutonomyLevel.DRAFT.rank == 1
    assert AutonomyLevel.ACT_WITH_APPROVAL.rank == 2
    assert AutonomyLevel.ACT_AUTO.rank == 3


def test_clamped_to_lowers_but_never_raises() -> None:
    # min(assistant, cap): a configured level ABOVE the cap is lowered to the cap …
    assert AutonomyLevel.ACT_AUTO.clamped_to(AutonomyLevel.DRAFT) is AutonomyLevel.DRAFT
    # … a level already at/below the cap is unchanged (the cap never raises it).
    assert AutonomyLevel.SUGGEST.clamped_to(AutonomyLevel.ACT_AUTO) is AutonomyLevel.SUGGEST
    assert AutonomyLevel.DRAFT.clamped_to(AutonomyLevel.DRAFT) is AutonomyLevel.DRAFT


# --- fixtures ---------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
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
        dave: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice  # owner in tenant A
        self.bob = bob  # backup owner in tenant A
        self.carol = carol  # owner in tenant B
        self.dave = dave  # backup owner in tenant B


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
    dave = await UserRepository(session, tb.id).create(
        email="dave@globex.test", password_hash="h", roles=[Role.MEMBER]
    )
    await session.commit()
    return _World(
        tenant_a=ta.id, tenant_b=tb.id, alice=alice.id, bob=bob.id, carol=carol.id, dave=dave.id
    )


def _assistants_service(
    session: AsyncSession, *, tenant_id: uuid.UUID, owner_id: uuid.UUID
) -> AssistantsService:
    return AssistantsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=(Role.MEMBER,),
        audit=AuditSink(AuditEventRepository(session, tenant_id)),
        request_id="req-test",
        source_ip="203.0.113.1",
    )


async def _set_cap(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID, cap: AutonomyLevel
) -> None:
    svc = AutonomyPolicyService(session, tenant_id=tenant_id)
    await svc.set_policy(
        max_autonomy=cap, actor_id=actor_id, request_id="req", source_ip="203.0.113.1"
    )
    await session.commit()


# --- service read/write + reader clamp -------------------------------------


async def test_service_default_is_no_ceiling(session: AsyncSession, world: _World) -> None:
    svc = AutonomyPolicyService(session, tenant_id=world.tenant_a)
    view = await svc.get_policy()
    assert view.is_default is True
    assert view.max_autonomy is AutonomyLevel.ACT_AUTO


async def test_service_set_and_reader_clamp(session: AsyncSession, world: _World) -> None:
    await _set_cap(session, tenant_id=world.tenant_a, actor_id=world.alice, cap=AutonomyLevel.DRAFT)
    reader = AutonomyPolicyReader(session, tenant_id=world.tenant_a)
    assert await reader.effective_cap() is AutonomyLevel.DRAFT
    # An assistant configured above the cap is lowered to it; one below is untouched.
    assert await reader.clamp(AutonomyLevel.ACT_AUTO) is AutonomyLevel.DRAFT
    assert await reader.clamp(AutonomyLevel.SUGGEST) is AutonomyLevel.SUGGEST


async def test_reader_is_tenant_scoped(session: AsyncSession, world: _World) -> None:
    """INV-1: a cap in tenant A never lowers tenant B's effective autonomy."""
    await _set_cap(
        session, tenant_id=world.tenant_a, actor_id=world.alice, cap=AutonomyLevel.SUGGEST
    )
    reader_b = AutonomyPolicyReader(session, tenant_id=world.tenant_b)
    # Tenant B has no cap ⇒ no ceiling: an act_auto assistant stays act_auto.
    assert await reader_b.effective_cap() is AutonomyLevel.ACT_AUTO
    assert await reader_b.clamp(AutonomyLevel.ACT_AUTO) is AutonomyLevel.ACT_AUTO


async def test_upsert_is_a_singleton(session: AsyncSession, world: _World) -> None:
    repo = TenantAutonomyPolicyRepository(session, world.tenant_a)
    await repo.upsert(max_autonomy=AutonomyLevel.DRAFT, updated_by=world.alice)
    await repo.upsert(max_autonomy=AutonomyLevel.ACT_WITH_APPROVAL, updated_by=world.alice)
    await session.commit()
    stored = await repo.get()
    assert stored is not None
    assert stored.max_autonomy is AutonomyLevel.ACT_WITH_APPROVAL  # updated in place


# --- AC-2: publish is rejected above the cap (negative) --------------------


async def test_publish_rejected_above_cap(session: AsyncSession, world: _World) -> None:
    """AC-2 (#218, INV-8): an assistant configured above the tenant cap cannot publish."""
    svc = _assistants_service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(
        name="Bold agent",
        autonomy_level=AutonomyLevel.ACT_AUTO,
        backup_owner_id=world.bob,
    )
    await session.commit()
    # The admin then caps the tenant at ``draft`` — below the assistant's act_auto.
    await _set_cap(session, tenant_id=world.tenant_a, actor_id=world.bob, cap=AutonomyLevel.DRAFT)

    with pytest.raises(ValidationError) as exc:
        await svc.publish(assistant.id)
    assert exc.value.status == 422
    assert exc.value.code == "autonomy_above_cap"


async def test_publish_allowed_at_or_below_cap(session: AsyncSession, world: _World) -> None:
    """AC-2 (#218): an assistant at/below the cap publishes normally."""
    await _set_cap(
        session,
        tenant_id=world.tenant_a,
        actor_id=world.bob,
        cap=AutonomyLevel.ACT_WITH_APPROVAL,
    )
    svc = _assistants_service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(
        name="Approval agent",
        autonomy_level=AutonomyLevel.ACT_WITH_APPROVAL,
        backup_owner_id=world.bob,
    )
    await session.commit()
    version = await svc.publish(assistant.id)
    assert version.version == 1


# --- AC-3: effective autonomy is visible on the assistant projection --------


async def test_effective_autonomy_reflects_the_cap(session: AsyncSession, world: _World) -> None:
    """AC-3 (#218): the assistant read shows min(configured, cap) so the library can display it."""
    svc = _assistants_service(session, tenant_id=world.tenant_a, owner_id=world.alice)
    assistant = await svc.create(name="Agent", autonomy_level=AutonomyLevel.ACT_AUTO)
    await session.commit()
    # No cap yet ⇒ effective == configured.
    fresh = await svc.get(assistant.id)
    assert fresh.autonomy_level is AutonomyLevel.ACT_AUTO
    assert fresh.effective_autonomy is AutonomyLevel.ACT_AUTO

    # Cap the tenant at ``draft`` — the CONFIGURED level is unchanged, but the EFFECTIVE
    # level the library shows drops to the ceiling.
    await _set_cap(session, tenant_id=world.tenant_a, actor_id=world.bob, cap=AutonomyLevel.DRAFT)
    capped = await svc.get(assistant.id)
    assert capped.autonomy_level is AutonomyLevel.ACT_AUTO  # configured unchanged
    assert capped.effective_autonomy is AutonomyLevel.DRAFT  # effective clamped


async def test_effective_autonomy_isolated_across_tenants(
    session: AsyncSession, world: _World
) -> None:
    """INV-1 (#218): tenant A's cap never lowers tenant B's assistant's effective autonomy."""
    await _set_cap(
        session, tenant_id=world.tenant_a, actor_id=world.alice, cap=AutonomyLevel.SUGGEST
    )
    svc_b = _assistants_service(session, tenant_id=world.tenant_b, owner_id=world.carol)
    assistant_b = await svc_b.create(name="B agent", autonomy_level=AutonomyLevel.ACT_AUTO)
    await session.commit()
    got_b = await svc_b.get(assistant_b.id)
    # Tenant B has no cap ⇒ effective == configured, unaffected by A's suggest cap.
    assert got_b.effective_autonomy is AutonomyLevel.ACT_AUTO
