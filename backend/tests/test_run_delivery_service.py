"""Run-delivery service tests — the in-app inbox + digest (issue #238, ADR-0015 §6).

Drives :mod:`app.services.run_delivery_service` **offline** over a SQLite DB (no
Postgres / Redis / model): a completed run produces an in-app delivery for its owner
(AC-1), a digest-configured schedule batches its runs into a periodic in-app digest
(AC-2), a delivery can be marked read, and a produce failure is written as a
``failed`` row — never a silent drop (AC-3). The mandatory negatives: a cross-tenant
delivery id → 404 (INV-1), another owner's delivery → 404 (INV-2).

Deliveries are produced by :func:`deliver_run` (the hook the run task calls on
completion) and read/mutated through :class:`RunDeliveryService`; runs are seeded
directly through the repository (executing the full agentic loop is
``test_runs_service``'s concern). A no-op audit sink captures the INV-6 events.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import NotFoundError
from app.db.base import Base
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    AuditEventRepository,
    RunDeliveryRepository,
    RunRepository,
    ScheduleRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    DigestCadence,
    KnowledgeScope,
    Role,
    RunDeliveryKind,
    RunDeliveryStatus,
    RunStatus,
    RunTrigger,
    ScheduleDelivery,
)
from app.domain.scheduling import cadence_from_cron
from app.services.assistants_service import config_from_assistant
from app.services.audit import AuditSink
from app.services.run_delivery_service import (
    RunDeliveryService,
    build_digest_for_tenant,
    deliver_run,
)

import app.db.models  # noqa: F401  isort: skip


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
        carol_id: uuid.UUID,
        assistant_a: uuid.UUID,
        version_a: uuid.UUID,
        assistant_b: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.carol_id = carol_id
        self.assistant_a = assistant_a
        self.version_a = version_a
        self.assistant_b = assistant_b


@pytest_asyncio.fixture
async def ctx() -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as seed:
            ta = await TenantRepository(seed).create(name="Acme")
            tb = await TenantRepository(seed).create(name="Globex")
            alice = await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            carol = await UserRepository(seed, tb.id).create(
                email="carol@globex.test", password_hash="x", roles=[Role.MEMBER]
            )
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
                assistant_id=assistant.id,
                version=1,
                author_id=alice.id,
                config=config_from_assistant(published),
            )
            b_assistants = AssistantRepository(seed, tb.id)
            b_assistant = await b_assistants.create(
                owner_id=carol.id,
                name="Globex",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=None,
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice_id=alice.id,
                bob_id=bob.id,
                carol_id=carol.id,
                assistant_a=assistant.id,
                version_a=version.id,
                assistant_b=b_assistant.id,
            )
    finally:
        await engine.dispose()


def _audit(session: AsyncSession, tenant_id: uuid.UUID) -> AuditSink:
    return AuditSink(AuditEventRepository(session, tenant_id))


async def _completed_run(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    owner_id: uuid.UUID,
    assistant_id: uuid.UUID,
    version_id: uuid.UUID | None,
    schedule_id: uuid.UUID | None = None,
    status: RunStatus = RunStatus.SUCCEEDED,
    summary: str | None = "The weekly summary is ready.",
    trigger: RunTrigger = RunTrigger.SCHEDULE,
):
    runs = RunRepository(session, tenant_id)
    run = await runs.create(
        owner_id=owner_id,
        assistant_id=assistant_id,
        assistant_version_id=version_id,
        trigger=trigger,
        schedule_id=schedule_id,
    )
    terminal = await runs.mark_terminal(
        run.id, status=status, finished_at=datetime.now(UTC), summary=summary
    )
    assert terminal is not None
    return terminal


# --- AC-1: a completed run creates a delivery for its owner ------------------


async def test_completed_run_creates_inbox_delivery_for_owner(ctx: _Ctx) -> None:
    """AC-1: a completed run lands in the owner's inbox with its summary + a run link."""
    async with ctx.sessionmaker() as session:
        run = await _completed_run(
            session,
            ctx.tenant_a,
            owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_a,
            version_id=ctx.version_a,
        )
        delivery = await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()

    assert delivery is not None
    assert delivery.recipient_id == ctx.alice_id
    assert delivery.run_id == run.id
    assert delivery.kind is RunDeliveryKind.INBOX
    assert delivery.status is RunDeliveryStatus.DELIVERED
    assert delivery.summary == "The weekly summary is ready."

    # And it appears in the owner's inbox via the read service.
    async with ctx.sessionmaker() as session:
        service = RunDeliveryService(
            session,
            tenant_id=ctx.tenant_a,
            recipient_id=ctx.alice_id,
            audit=_audit(session, ctx.tenant_a),
            request_id="req",
            source_ip="ip",
        )
        page = await service.list_(cursor=None, limit=20)
        assert {d.id for d in page.items} == {delivery.id}


async def test_deliver_run_is_idempotent(ctx: _Ctx) -> None:
    """A redelivered run task never double-notifies (INV-8, at-least-once safe)."""
    async with ctx.sessionmaker() as session:
        run = await _completed_run(
            session, ctx.tenant_a, owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_a, version_id=ctx.version_a,
        )
        first = await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        second = await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()

    assert first is not None
    # The second call produced no new inbox delivery.
    assert second is None
    async with ctx.sessionmaker() as session:
        rows = await RunDeliveryRepository(session, ctx.tenant_a).list_for_recipient_page(
            ctx.alice_id, limit=20
        )
        assert len(rows) == 1


async def test_failed_run_still_delivered_never_silent(ctx: _Ctx) -> None:
    """AC-3: a failed run still reaches the inbox (visible + retryable), never silent."""
    async with ctx.sessionmaker() as session:
        run = await _completed_run(
            session, ctx.tenant_a, owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_a, version_id=ctx.version_a,
            status=RunStatus.FAILED, summary=None,
        )
        delivery = await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()

    assert delivery is not None
    assert delivery.run_id == run.id
    # The delivery is a queryable row the owner sees; the run detail carries the error.
    assert delivery.status is RunDeliveryStatus.DELIVERED


# --- AC-2: low-frequency runs batch into a digest ---------------------------


async def _schedule_with_digest(ctx: _Ctx, session: AsyncSession) -> uuid.UUID:
    schedule = await ScheduleRepository(session, ctx.tenant_a).create(
        owner_id=ctx.alice_id,
        assistant_id=ctx.assistant_a,
        cadence=cadence_from_cron("0 9 * * 1"),
        timezone="UTC",
        delivery=ScheduleDelivery(inbox=False, digest=DigestCadence.DAILY),
    )
    return schedule.id


async def test_digest_schedule_batches_runs(ctx: _Ctx) -> None:
    """AC-2: a digest-configured schedule's runs are pending until the digest fires."""
    async with ctx.sessionmaker() as session:
        schedule_id = await _schedule_with_digest(ctx, session)
        for _ in range(3):
            run = await _completed_run(
                session, ctx.tenant_a, owner_id=ctx.alice_id,
                assistant_id=ctx.assistant_a, version_id=ctx.version_a,
                schedule_id=schedule_id,
            )
            await deliver_run(
                session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
            )
        await session.commit()

    # Three PENDING digest deliveries, no immediate inbox delivery (inbox=false).
    async with ctx.sessionmaker() as session:
        deliveries = RunDeliveryRepository(session, ctx.tenant_a)
        pending = await deliveries.list_pending_digest(limit=100)
        assert len(pending) == 3
        assert all(d.kind is RunDeliveryKind.DIGEST for d in pending)
        assert all(d.status is RunDeliveryStatus.PENDING for d in pending)

    # The digest beat rolls them into DELIVERED (surfaced in the inbox as one batch).
    async with ctx.sessionmaker() as session:
        batched = await build_digest_for_tenant(
            session, tenant_id=ctx.tenant_a, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()
        assert batched == 3

    async with ctx.sessionmaker() as session:
        deliveries = RunDeliveryRepository(session, ctx.tenant_a)
        assert await deliveries.list_pending_digest(limit=100) == []
        page = await deliveries.list_for_recipient_page(ctx.alice_id, limit=100)
        assert len(page) == 3
        assert all(d.status is RunDeliveryStatus.DELIVERED for d in page)


async def test_digest_batch_is_idempotent(ctx: _Ctx) -> None:
    """A second digest sweep over an already-delivered batch does nothing."""
    async with ctx.sessionmaker() as session:
        schedule_id = await _schedule_with_digest(ctx, session)
        run = await _completed_run(
            session, ctx.tenant_a, owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_a, version_id=ctx.version_a, schedule_id=schedule_id,
        )
        await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        assert await build_digest_for_tenant(session, tenant_id=ctx.tenant_a) == 1
        await session.commit()
    async with ctx.sessionmaker() as session:
        assert await build_digest_for_tenant(session, tenant_id=ctx.tenant_a) == 0


# --- mark-read --------------------------------------------------------------


async def test_mark_read_stamps_and_is_idempotent(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        run = await _completed_run(
            session, ctx.tenant_a, owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_a, version_id=ctx.version_a,
        )
        delivery = await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()
    assert delivery is not None

    async with ctx.sessionmaker() as session:
        service = RunDeliveryService(
            session, tenant_id=ctx.tenant_a, recipient_id=ctx.alice_id,
            audit=_audit(session, ctx.tenant_a), request_id="req", source_ip="ip",
        )
        read = await service.mark_read(delivery.id)
        await session.commit()
        assert read.status is RunDeliveryStatus.READ
        assert read.read_at is not None
        first_read_at = read.read_at

    # Re-marking is idempotent — read_at unchanged.
    async with ctx.sessionmaker() as session:
        service = RunDeliveryService(
            session, tenant_id=ctx.tenant_a, recipient_id=ctx.alice_id,
            audit=_audit(session, ctx.tenant_a), request_id="req", source_ip="ip",
        )
        again = await service.mark_read(delivery.id)
        assert again.status is RunDeliveryStatus.READ
        # Idempotent: the first open's timestamp stands (compare tz-naive — SQLite
        # drops tzinfo on reload; the wall-clock value is unchanged).
        assert again.read_at is not None
        assert again.read_at.replace(tzinfo=None) == first_read_at.replace(tzinfo=None)


# --- Negatives: INV-1 / INV-2 -----------------------------------------------


async def test_cross_tenant_delivery_mark_read_is_404(ctx: _Ctx) -> None:
    """INV-1: a delivery in another tenant is 404 (existence non-disclosure)."""
    # Carol (tenant B) has a completed run + delivery.
    async with ctx.sessionmaker() as session:
        carol_run = await _completed_run(
            session, ctx.tenant_b, owner_id=ctx.carol_id,
            assistant_id=ctx.assistant_b, version_id=None, trigger=RunTrigger.MANUAL,
        )
        carol_delivery = await deliver_run(
            session, tenant_id=ctx.tenant_b, run=carol_run, audit=_audit(session, ctx.tenant_b)
        )
        await session.commit()
    assert carol_delivery is not None

    # Alice (tenant A) cannot mark carol's delivery read — 404.
    async with ctx.sessionmaker() as session:
        service = RunDeliveryService(
            session, tenant_id=ctx.tenant_a, recipient_id=ctx.alice_id,
            audit=_audit(session, ctx.tenant_a), request_id="req", source_ip="ip",
        )
        with pytest.raises(NotFoundError):
            await service.mark_read(carol_delivery.id)


async def test_other_owner_delivery_mark_read_is_404(ctx: _Ctx) -> None:
    """INV-2: a delivery addressed to another user in the same tenant is 404, not 403."""
    async with ctx.sessionmaker() as session:
        run = await _completed_run(
            session, ctx.tenant_a, owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_a, version_id=ctx.version_a,
        )
        delivery = await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()
    assert delivery is not None

    # Bob (same tenant, different user) cannot see alice's delivery — 404.
    async with ctx.sessionmaker() as session:
        service = RunDeliveryService(
            session, tenant_id=ctx.tenant_a, recipient_id=ctx.bob_id,
            audit=_audit(session, ctx.tenant_a), request_id="req", source_ip="ip",
        )
        with pytest.raises(NotFoundError):
            await service.mark_read(delivery.id)
        # And bob's inbox list is empty (the delivery is alice's).
        page = await service.list_(cursor=None, limit=20)
        assert page.items == []


async def test_manual_run_delivers_to_inbox_by_default(ctx: _Ctx) -> None:
    """A manual run (no schedule) always lands in the inbox (the v1 default)."""
    async with ctx.sessionmaker() as session:
        run = await _completed_run(
            session, ctx.tenant_a, owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_a, version_id=ctx.version_a,
            schedule_id=None, trigger=RunTrigger.MANUAL,
        )
        delivery = await deliver_run(
            session, tenant_id=ctx.tenant_a, run=run, audit=_audit(session, ctx.tenant_a)
        )
        await session.commit()
    assert delivery is not None
    assert delivery.kind is RunDeliveryKind.INBOX
    assert delivery.schedule_id is None
