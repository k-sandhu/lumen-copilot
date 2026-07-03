"""Scheduler fire-dispatcher tests — overlap / concurrency / rate gates (ADR-0015 §5, #236).

Drives :func:`app.tasks.scheduler._dispatch_fire` over an offline in-memory SQLite DB
(no Redis / Celery broker). The Celery enqueue leg (``enqueue_run``) is stubbed so no
broker is touched; a fake rate limiter forces the throttle path. Covers the mandatory
overlap negative — **overlap=skip prevents a second concurrent run** — plus the
per-tenant concurrency + rate deferrals (a fire is deferred, never dropped; the
schedule keeps ticking) and the happy path (a fire creates a queued schedule-triggered
run + records the fire summary).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    RunRepository,
    ScheduleRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    KnowledgeScope,
    OverlapPolicy,
    Role,
    RunStatus,
    RunTrigger,
)
from app.domain.scheduling import cadence_from_cron
from app.services.assistants_service import config_from_assistant
from app.tasks import scheduler

import app.db.models  # noqa: F401  isort: skip

_NY = "America/New_York"


class _AlwaysLimiter:
    """A rate limiter that always admits (the happy path)."""

    def try_acquire(self, tenant_id: uuid.UUID) -> bool:
        return True


class _NeverLimiter:
    """A rate limiter that always throttles (forces the deferral path)."""

    def try_acquire(self, tenant_id: uuid.UUID) -> bool:
        return False


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_a: uuid.UUID,
        alice_id: uuid.UUID,
        assistant_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_a = tenant_a
        self.alice_id = alice_id
        self.assistant_id = assistant_id


@pytest_asyncio.fixture
async def ctx(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    # ``_dispatch_fire`` opens its session via ``tenant_session_scope`` → point it here.
    monkeypatch.setattr("app.db.session.get_sessionmaker", lambda settings=None: factory)
    # Stub the Celery enqueue so no broker is touched (assert it was called via a flag).
    enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        "app.tasks.run_assistant.enqueue_run",
        lambda run_id, tenant_id: enqueued.append((run_id, tenant_id)),
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as seed:
            ta = await TenantRepository(seed).create(name="Acme")
            alice = await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            assistants = AssistantRepository(seed, ta.id)
            published = await assistants.create(
                owner_id=alice.id, name="Weekly", knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(), autonomy_level=AutonomyLevel.SUGGEST, backup_owner_id=bob.id,
            )
            await assistants.update(published.id, fields={"status": AssistantStatus.PUBLISHED})
            head = await assistants.get(published.id)
            await AssistantVersionRepository(seed, ta.id).add(
                assistant_id=published.id, version=1, author_id=alice.id,
                config=config_from_assistant(head),
            )
            await seed.commit()
            c = _Ctx(sessionmaker=factory, tenant_a=ta.id, alice_id=alice.id,
                     assistant_id=published.id)
            c.enqueued = enqueued  # type: ignore[attr-defined]
            yield c
    finally:
        await engine.dispose()


async def _make_schedule(
    ctx: _Ctx, *, overlap: OverlapPolicy = OverlapPolicy.SKIP, enabled: bool = True
) -> uuid.UUID:
    async with ctx.sessionmaker() as session:
        schedule = await ScheduleRepository(session, ctx.tenant_a).create(
            owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 8 * * *"),
            timezone=_NY,
            input_params={"prompt": "run"},
            overlap_policy=overlap,
            enabled=enabled,
        )
        await session.commit()
        return schedule.id


async def _seed_active_run(ctx: _Ctx, schedule_id: uuid.UUID) -> uuid.UUID:
    """Seed a still-active (queued) run for a schedule (simulates a prior fire in flight)."""
    async with ctx.sessionmaker() as session:
        head = await AssistantVersionRepository(session, ctx.tenant_a).get_head(ctx.assistant_id)
        run = await RunRepository(session, ctx.tenant_a).create(
            owner_id=ctx.alice_id,
            assistant_id=ctx.assistant_id,
            assistant_version_id=head.id,
            trigger=RunTrigger.SCHEDULE,
            schedule_id=schedule_id,
        )
        await session.commit()
        return run.id


async def _active_run_count(ctx: _Ctx, schedule_id: uuid.UUID) -> int:
    async with ctx.sessionmaker() as session:
        return await RunRepository(session, ctx.tenant_a).count_active_for_schedule(schedule_id)


def _settings() -> Settings:
    from app.core.config import get_settings

    return get_settings()


# --- happy path -------------------------------------------------------------


async def test_fire_enqueues_run_and_records_summary(ctx: _Ctx) -> None:
    schedule_id = await _make_schedule(ctx)
    outcome = await scheduler._dispatch_fire(
        schedule_id, ctx.tenant_a, settings=_settings(), rate_limiter=_AlwaysLimiter()
    )
    assert outcome == "enqueued"
    # A queued schedule-triggered run now exists, linked to the schedule.
    assert await _active_run_count(ctx, schedule_id) == 1
    async with ctx.sessionmaker() as session:
        runs = await RunRepository(session, ctx.tenant_a).list_for_owner_page(
            ctx.alice_id, limit=10, schedule_id=schedule_id
        )
        assert len(runs) == 1
        assert runs[0].trigger is RunTrigger.SCHEDULE
        assert runs[0].inputs == {"prompt": "run"}  # the schedule's params snapshotted
        # The fire summary + advanced next_run_at were recorded.
        schedule = await ScheduleRepository(session, ctx.tenant_a).get(schedule_id)
        assert schedule.last_status is RunStatus.QUEUED
        assert schedule.last_run_at is not None
        assert schedule.next_run_at is not None
    # The Celery task was enqueued after-commit (broker leg stubbed).
    assert (runs[0].id, ctx.tenant_a) in ctx.enqueued  # type: ignore[attr-defined]


# --- overlap = skip (the mandatory negative) --------------------------------


async def test_overlap_skip_prevents_second_concurrent_run(ctx: _Ctx) -> None:
    """A fire while a prior run is still active (policy=skip) enqueues NOTHING (ADR-0015 §5)."""
    schedule_id = await _make_schedule(ctx, overlap=OverlapPolicy.SKIP)
    await _seed_active_run(ctx, schedule_id)  # a prior run is still queued
    assert await _active_run_count(ctx, schedule_id) == 1

    outcome = await scheduler._dispatch_fire(
        schedule_id, ctx.tenant_a, settings=_settings(), rate_limiter=_AlwaysLimiter()
    )
    assert outcome == "skipped_overlap"
    # Still exactly ONE active run — the fire did not stack a second (the whole point).
    assert await _active_run_count(ctx, schedule_id) == 1
    # A skipped fire does not overwrite the prior run's last_status, but advances next_run_at.
    async with ctx.sessionmaker() as session:
        schedule = await ScheduleRepository(session, ctx.tenant_a).get(schedule_id)
        assert schedule.next_run_at is not None


async def test_overlap_allow_runs_concurrently(ctx: _Ctx) -> None:
    """With policy=allow, a fire enqueues a second run even while one is active."""
    schedule_id = await _make_schedule(ctx, overlap=OverlapPolicy.ALLOW)
    await _seed_active_run(ctx, schedule_id)
    outcome = await scheduler._dispatch_fire(
        schedule_id, ctx.tenant_a, settings=_settings(), rate_limiter=_AlwaysLimiter()
    )
    assert outcome == "enqueued"
    assert await _active_run_count(ctx, schedule_id) == 2  # both concurrent


# --- rate / concurrency deferral --------------------------------------------


async def test_rate_cap_defers_fire_without_dropping(ctx: _Ctx) -> None:
    """Over the per-tenant rate window a fire is deferred (no run), never dropped."""
    schedule_id = await _make_schedule(ctx)
    outcome = await scheduler._dispatch_fire(
        schedule_id, ctx.tenant_a, settings=_settings(), rate_limiter=_NeverLimiter()
    )
    assert outcome == "deferred_rate"
    assert await _active_run_count(ctx, schedule_id) == 0  # nothing enqueued
    async with ctx.sessionmaker() as session:
        schedule = await ScheduleRepository(session, ctx.tenant_a).get(schedule_id)
        assert schedule.next_run_at is not None  # keeps ticking


async def test_concurrency_cap_defers_fire(ctx: _Ctx) -> None:
    """At the per-tenant in-flight cap a fire is deferred (ADR-0015 §5).

    Uses ``overlap=allow`` so the overlap gate passes (it is not the skip case) and
    the concurrency cap is what defers the fire.
    """
    schedule_id = await _make_schedule(ctx, overlap=OverlapPolicy.ALLOW)
    await _seed_active_run(ctx, schedule_id)  # one in-flight run

    # Force the cap to 1 so the single active run trips it.
    tight = _settings().model_copy(update={"run_max_in_flight_per_tenant": 1})
    outcome = await scheduler._dispatch_fire(
        schedule_id, ctx.tenant_a, settings=tight, rate_limiter=_AlwaysLimiter()
    )
    assert outcome == "deferred_concurrency"
    assert await _active_run_count(ctx, schedule_id) == 1  # no new run


# --- disabled / missing schedule --------------------------------------------


async def test_fire_on_disabled_schedule_is_noop(ctx: _Ctx) -> None:
    schedule_id = await _make_schedule(ctx, enabled=False)
    outcome = await scheduler._dispatch_fire(
        schedule_id, ctx.tenant_a, settings=_settings(), rate_limiter=_AlwaysLimiter()
    )
    assert outcome == "no_schedule"
    assert await _active_run_count(ctx, schedule_id) == 0


async def test_fire_on_missing_schedule_is_noop(ctx: _Ctx) -> None:
    outcome = await scheduler._dispatch_fire(
        uuid.uuid4(), ctx.tenant_a, settings=_settings(), rate_limiter=_AlwaysLimiter()
    )
    assert outcome == "no_schedule"
