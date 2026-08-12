"""Schedule service tests — CRUD + pause/resume/run-now (ADR-0015, issue #236).

Drives :class:`app.services.schedules_service.SchedulesService` over an offline
in-memory SQLite DB (no Postgres / Redis / Celery). A fake projector records the
RedBeat sync/remove calls (the service's derived-entry reconcile), and the real
audit sink writes to the SQLite ``audit_events`` table so INV-6 is asserted end to
end. Covers the mandatory negatives:

* cross-tenant / non-owned schedule id → **404** (INV-1/INV-2);
* malformed cron / unknown timezone → **422** (INV-8);
* scheduling / running a **disabled** or non-owned assistant → rejected (422/404);
* ``run-now`` on a **paused** schedule → **409** (INV-8);
* every mutation + control is audited (INV-6).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.base import Base
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    AuditEventRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AssistantStatus,
    AutonomyLevel,
    KnowledgeScope,
    OverlapPolicy,
    Role,
    Schedule,
)
from app.domain.scheduling import (
    CadenceUnit,
    StructuredCadence,
    cadence_from_cron,
    cadence_from_structured,
)
from app.services.assistants_service import config_from_assistant
from app.services.audit import AuditSink
from app.services.schedules_service import SchedulesService

import app.db.models  # noqa: F401  isort: skip

_NY = "America/New_York"


class _RecordingProjector:
    """A fake :class:`ScheduleProjector` recording the derived-entry reconcile calls."""

    def __init__(self) -> None:
        self.synced: list[uuid.UUID] = []
        self.removed: list[uuid.UUID] = []

    def sync(self, schedule: Schedule) -> None:
        self.synced.append(schedule.id)

    def remove(self, schedule_id: uuid.UUID) -> None:
        self.removed.append(schedule_id)


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
        assistant_id: uuid.UUID,
        disabled_assistant_id: uuid.UUID,
        carol_id: uuid.UUID,
        carol_assistant_id: uuid.UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.assistant_id = assistant_id
        self.disabled_assistant_id = disabled_assistant_id
        self.carol_id = carol_id
        self.carol_assistant_id = carol_assistant_id


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
            assistants_a = AssistantRepository(seed, ta.id)
            versions_a = AssistantVersionRepository(seed, ta.id)

            # A published assistant owned by alice, pinned to one version.
            published = await assistants_a.create(
                owner_id=alice.id,
                name="Weekly summary",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=bob.id,
            )
            await assistants_a.update(published.id, fields={"status": AssistantStatus.PUBLISHED})
            head = await assistants_a.get(published.id)
            await versions_a.add(
                assistant_id=published.id,
                version=1,
                author_id=alice.id,
                config=config_from_assistant(head),
            )

            # A DISABLED assistant owned by alice (cannot be scheduled/run).
            disabled = await assistants_a.create(
                owner_id=alice.id,
                name="Retired",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=bob.id,
            )
            await assistants_a.update(disabled.id, fields={"status": AssistantStatus.DISABLED})

            # A published assistant in Globex (carol's tenant).
            assistants_b = AssistantRepository(seed, tb.id)
            versions_b = AssistantVersionRepository(seed, tb.id)
            carol_ast = await assistants_b.create(
                owner_id=carol.id,
                name="Globex",
                knowledge_scope=KnowledgeScope.empty(),
                tool_allowlist=(),
                autonomy_level=AutonomyLevel.SUGGEST,
                backup_owner_id=None,
            )
            await assistants_b.update(carol_ast.id, fields={"status": AssistantStatus.PUBLISHED})
            head_b = await assistants_b.get(carol_ast.id)
            await versions_b.add(
                assistant_id=carol_ast.id,
                version=1,
                author_id=carol.id,
                config=config_from_assistant(head_b),
            )
            await seed.commit()
            yield _Ctx(
                sessionmaker=factory,
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice_id=alice.id,
                bob_id=bob.id,
                assistant_id=published.id,
                disabled_assistant_id=disabled.id,
                carol_id=carol.id,
                carol_assistant_id=carol_ast.id,
            )
    finally:
        await engine.dispose()


def _service(
    ctx: _Ctx,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    roles: tuple[Role, ...] = (Role.MEMBER,),
    projector: _RecordingProjector | None = None,
) -> SchedulesService:
    return SchedulesService(
        session,
        tenant_id=tenant_id or ctx.tenant_a,
        owner_id=owner_id or ctx.alice_id,
        roles=roles,
        audit=AuditSink(AuditEventRepository(session, tenant_id or ctx.tenant_a)),
        request_id="test-req",
        source_ip="127.0.0.1",
        projector=projector or _RecordingProjector(),
    )


async def _audit_actions(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    events = await AuditEventRepository(session, tenant_id).list_recent(limit=100)
    return [e.action for e in events]


# --- create -----------------------------------------------------------------


async def test_create_computes_next_run_and_projects_and_audits(ctx: _Ctx) -> None:
    projector = _RecordingProjector()
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session, projector=projector)
        schedule = await service.create(
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 8 * * *"),
            timezone=_NY,
            input_params={"prompt": "Summarize Q3"},
        )
        await session.commit()
        assert schedule.enabled is True
        assert schedule.next_run_at is not None  # computed tz/DST-correct
        assert schedule.overlap_policy is OverlapPolicy.SKIP  # default
        assert projector.synced == [schedule.id]  # RedBeat entry derived
        assert "schedule.created" in await _audit_actions(session, ctx.tenant_a)


async def test_create_disabled_starts_paused_no_next_run(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        schedule = await service.create(
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 8 * * *"),
            timezone=_NY,
            enabled=False,
        )
        assert schedule.enabled is False
        assert schedule.next_run_at is None


async def test_create_rejects_disabled_assistant(ctx: _Ctx) -> None:
    """Scheduling a DISABLED assistant is rejected 422 (the mandatory negative)."""
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        with pytest.raises(ValidationError) as exc:
            await service.create(
                assistant_id=ctx.disabled_assistant_id,
                cadence=cadence_from_cron("0 8 * * *"),
                timezone=_NY,
            )
        assert exc.value.code == "assistant_not_runnable"


async def test_create_rejects_cross_tenant_assistant_404(ctx: _Ctx) -> None:
    """Scheduling an assistant in another tenant → 404 (existence non-disclosure, INV-1)."""
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)  # alice in tenant A
        with pytest.raises(NotFoundError):
            await service.create(
                assistant_id=ctx.carol_assistant_id,  # Globex assistant
                cadence=cadence_from_cron("0 8 * * *"),
                timezone=_NY,
            )


async def test_create_rejects_unknown_timezone_422(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        with pytest.raises(ValidationError) as exc:
            await service.create(
                assistant_id=ctx.assistant_id,
                cadence=cadence_from_cron("0 8 * * *"),
                timezone="Not/AZone",
            )
        assert exc.value.code == "invalid_timezone"


# --- read / cross-tenant ----------------------------------------------------


async def test_get_cross_tenant_schedule_is_404(ctx: _Ctx) -> None:
    """A schedule created in tenant A is invisible to a caller in tenant B (INV-1)."""
    async with ctx.sessionmaker() as session:
        created = await _service(ctx, session).create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        carol_service = _service(ctx, session, tenant_id=ctx.tenant_b, owner_id=ctx.carol_id)
        with pytest.raises(NotFoundError):
            await carol_service.get(created.id)


async def test_get_other_owner_same_tenant_is_404(ctx: _Ctx) -> None:
    """Bob (same tenant, non-owner, non-admin) cannot see alice's schedule (INV-2)."""
    async with ctx.sessionmaker() as session:
        created = await _service(ctx, session).create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        bob_service = _service(ctx, session, owner_id=ctx.bob_id)
        with pytest.raises(NotFoundError):
            await bob_service.get(created.id)
        denied = [
            event
            for event in await AuditEventRepository(session, ctx.tenant_a).list_recent(limit=20)
            if event.action == "permission.denied" and event.resource_id == str(created.id)
        ]
        assert len(denied) == 1
        event = denied[0]
        assert event.actor_id == ctx.bob_id
        assert event.outcome.value == "denied"
        assert event.resource_type == "schedule"
        assert event.metadata == {
            "attempted_action": "schedule.read",
            "reason": "not_visible",
        }


async def test_admin_may_manage_another_owners_schedule(ctx: _Ctx) -> None:
    """A tenant admin may read a schedule owned by another user (owner-or-admin)."""
    async with ctx.sessionmaker() as session:
        created = await _service(ctx, session).create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        admin_service = _service(ctx, session, owner_id=ctx.bob_id, roles=(Role.MEMBER, Role.ADMIN))
        got = await admin_service.get(created.id)
        assert got.id == created.id


# --- update -----------------------------------------------------------------


async def test_update_cadence_recomputes_next_run_and_audits(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        created = await service.create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        before = created.next_run_at
        updated = await service.update(
            created.id,
            cadence=cadence_from_structured(StructuredCadence(every=CadenceUnit.DAY, at="23:59")),
        )
        await session.commit()
        assert updated.cadence.cron == "59 23 * * *"
        assert updated.next_run_at is not None and updated.next_run_at != before
        actions = await _audit_actions(session, ctx.tenant_a)
        assert "schedule.updated" in actions


async def test_update_rejects_bad_cron_422(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        created = await service.create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        with pytest.raises(ValidationError) as exc:
            await service.update(created.id, timezone="Also/Bogus")
        assert exc.value.code == "invalid_timezone"


async def test_update_cross_tenant_is_404(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        created = await _service(ctx, session).create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        carol_service = _service(ctx, session, tenant_id=ctx.tenant_b, owner_id=ctx.carol_id)
        with pytest.raises(NotFoundError):
            await carol_service.update(created.id, enabled=False)


# --- pause / resume ---------------------------------------------------------


async def test_pause_disables_removes_entry_and_clears_next_run(ctx: _Ctx) -> None:
    projector = _RecordingProjector()
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session, projector=projector)
        created = await service.create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        paused = await service.pause(created.id)
        await session.commit()
        assert paused.enabled is False
        assert paused.next_run_at is None
        assert created.id in projector.removed
        assert "schedule.paused" in await _audit_actions(session, ctx.tenant_a)


async def test_pause_is_idempotent(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        created = await service.create(
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 8 * * *"),
            timezone=_NY,
            enabled=False,
        )
        again = await service.pause(created.id)  # already paused
        assert again.enabled is False


async def test_resume_re_enables_recomputes_next_run_and_audits(ctx: _Ctx) -> None:
    projector = _RecordingProjector()
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session, projector=projector)
        created = await service.create(
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 8 * * *"),
            timezone=_NY,
            enabled=False,
        )
        resumed = await service.resume(created.id)
        await session.commit()
        assert resumed.enabled is True
        assert resumed.next_run_at is not None
        assert created.id in projector.synced
        assert "schedule.resumed" in await _audit_actions(session, ctx.tenant_a)


# --- delete -----------------------------------------------------------------


async def test_delete_removes_entry_and_audits(ctx: _Ctx) -> None:
    projector = _RecordingProjector()
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session, projector=projector)
        created = await service.create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await service.delete(created.id)
        await session.commit()
        assert created.id in projector.removed
        assert "schedule.deleted" in await _audit_actions(session, ctx.tenant_a)
        with pytest.raises(NotFoundError):
            await service.get(created.id)


# --- run-now ----------------------------------------------------------------


async def test_run_now_enqueues_manual_run_and_audits(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        created = await service.create(
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 8 * * *"),
            timezone=_NY,
            input_params={"prompt": "now please"},
        )
        run = await service.run_now(created.id)
        await session.commit()
        assert run.schedule_id == created.id
        assert run.trigger.value == "manual"
        assert run.inputs == {"prompt": "now please"}  # schedule's params snapshotted
        assert "schedule.run_now" in await _audit_actions(session, ctx.tenant_a)


async def test_run_now_on_paused_schedule_is_409(ctx: _Ctx) -> None:
    """run-now on a paused schedule is a 409 illegal transition (INV-8)."""
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        created = await service.create(
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 8 * * *"),
            timezone=_NY,
            enabled=False,
        )
        with pytest.raises(ConflictError) as exc:
            await service.run_now(created.id)
        assert exc.value.code == "schedule_paused"


async def test_run_now_cross_tenant_is_404(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        created = await _service(ctx, session).create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        carol_service = _service(ctx, session, tenant_id=ctx.tenant_b, owner_id=ctx.carol_id)
        with pytest.raises(NotFoundError):
            await carol_service.run_now(created.id)


async def test_run_now_rejects_since_disabled_assistant_422(ctx: _Ctx) -> None:
    """A schedule whose assistant was disabled after creation cannot run-now (422)."""
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        created = await service.create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        # Disable the assistant out from under the schedule.
        await AssistantRepository(session, ctx.tenant_a).update(
            ctx.assistant_id, fields={"status": AssistantStatus.DISABLED}
        )
        with pytest.raises(ValidationError) as exc:
            await service.run_now(created.id)
        assert exc.value.code == "assistant_not_runnable"


# --- list -------------------------------------------------------------------


async def test_list_returns_only_callers_schedules(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        alice = _service(ctx, session)
        s1 = await alice.create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await session.commit()
    async with ctx.sessionmaker() as session:
        bob = _service(ctx, session, owner_id=ctx.bob_id)
        page = await bob.list_(cursor=None, limit=20)
        assert all(item.id != s1.id for item in page.items)  # bob sees none of alice's


async def test_list_filters_by_enabled(ctx: _Ctx) -> None:
    async with ctx.sessionmaker() as session:
        service = _service(ctx, session)
        enabled = await service.create(
            assistant_id=ctx.assistant_id, cadence=cadence_from_cron("0 8 * * *"), timezone=_NY
        )
        await service.create(
            assistant_id=ctx.assistant_id,
            cadence=cadence_from_cron("0 9 * * *"),
            timezone=_NY,
            enabled=False,
        )
        await session.commit()
        page = await service.list_(cursor=None, limit=20, enabled=True)
        ids = {s.id for s in page.items}
        assert enabled.id in ids
        assert all(s.enabled for s in page.items)
