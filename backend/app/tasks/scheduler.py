"""Dynamic per-tenant Beat scheduler — celery-redbeat wiring + the fire dispatcher (#236).

The scheduler *mechanism* half of the scheduled-runs capability (ADR-0015 §1/§4).
**The only module that owns the Beat wiring** (ADR-0004: ``tasks/`` owns background
jobs; RedBeat is a Beat *scheduler backend* over the existing Redis broker, not a new
datastore — no new §6 boundary row). Three concerns live here:

* **Projection** (:class:`RedBeatScheduleProjector`) — the ``schedules`` table is the
  source of truth (Postgres authoritative); each enabled row projects into **one**
  RedBeat entry keyed by the schedule id, firing ``lumen.fire_schedule`` at the
  schedule's cadence in its timezone. Pausing/deleting removes the entry;
  editing re-derives it. Postgres survives a Redis flush, so a small reconcile step
  rebuilds a lost entry from the table (ADR-0015 §1). The service calls this seam
  through a :class:`ScheduleProjector` Protocol so the offline tests pass a fake.
* **Fire dispatcher** (:func:`fire_schedule`) — the Celery task RedBeat fires. It runs
  the **overlap / concurrency / rate** gates (ADR-0015 §5) *before* enqueuing the
  headless run (``lumen.run_assistant`` via ``enqueue_manual_run`` → ``enqueue_run``),
  then records the fire summary + the recomputed ``next_run_at``. A transient enqueue
  failure retries with backoff; a run **never silently drops**.
* **Beat config** (:func:`configure_beat` / :data:`REDBEAT_CONF`) — the settings the
  ``celery -A app.tasks.celery_app beat`` process reads (the RedBeat scheduler class,
  key prefix, lock timeout) + the boot reconcile that rebuilds entries from Postgres.

Timezone/DST correctness lives in the pure domain (``app.domain.scheduling`` —
``compute_next_run``); RedBeat's crontab carries the schedule's tz so its own fire
math and our ``next_run_at`` agree (Celery's clock stays UTC, ``enable_utc=True``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repositories import (
    RunRepository,
    ScheduleReconcileRepository,
    ScheduleRepository,
)
from app.db.session import session_scope, tenant_session_scope
from app.db.tenant_context import bind_bypass
from app.domain.entities import OverlapPolicy, RunStatus, Schedule
from app.domain.scheduling import compute_next_run
from app.tasks.celery_app import celery_app
from app.tasks.rate_limit import RateLimiter, RedisFixedWindowRateLimiter
from app.tasks.runner import run_task

log = structlog.get_logger(__name__)

# The RedBeat entry key for a schedule (namespaced under the configured prefix, set
# on the celery conf). One entry per schedule id; toggling ``enabled`` adds/removes it.
_ENTRY_NAME_PREFIX = "schedule:"

# Redis keyspace for the per-tenant run-enqueue rate limiter (distinct from the
# connector-sync + web-search limiters that share the same Redis).
_RUN_RATE_KEY_PREFIX = "lumen:ratelimit:run"


def _entry_name(schedule_id: UUID) -> str:
    return f"{_ENTRY_NAME_PREFIX}{schedule_id}"


# --- Projection seam (service → scheduler) ----------------------------------


class ScheduleProjector(Protocol):
    """The seam the service uses to reconcile a schedule's derived Beat entry (ADR-0015 §1).

    ``sync`` (re)derives the RedBeat entry for an **enabled** schedule and removes it
    for a **disabled** one; ``remove`` deletes the entry outright (on delete). Both are
    **best-effort** against Redis — a transient Redis outage must never turn a CRUD
    request into a 500 (Postgres remains the source of truth; the boot reconcile
    rebuilds a missed entry). The offline tests pass a fake recording projector.
    """

    def sync(self, schedule: Schedule) -> None: ...

    def remove(self, schedule_id: UUID) -> None: ...


class NullScheduleProjector:
    """A no-op projector — the default when RedBeat/Redis is not wired (tests, CLI).

    Keeps the service usable without a running Redis (Postgres is authoritative; the
    Beat boot reconcile derives entries from the table). The service's behaviour is
    identical; only the derived Redis entry is skipped.
    """

    def sync(self, schedule: Schedule) -> None:  # noqa: D401 — no-op
        return None

    def remove(self, schedule_id: UUID) -> None:  # noqa: D401 — no-op
        return None


class RedBeatScheduleProjector:
    """Projects ``schedules`` rows into RedBeat entries in the existing Redis (ADR-0015 §1).

    Best-effort: a Redis error is logged and swallowed (Postgres is the source of
    truth; the boot reconcile rebuilds any missed entry) so a CRUD request never
    fails on a transient Redis blip. The RedBeat crontab carries the schedule's IANA
    timezone so RedBeat's own next-fire math is DST-correct and agrees with our
    ``compute_next_run``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def sync(self, schedule: Schedule) -> None:
        if not schedule.enabled:
            self.remove(schedule.id)
            return
        try:
            from zoneinfo import ZoneInfo

            from celery.schedules import crontab
            from redbeat import RedBeatSchedulerEntry

            minute, hour, dom, month, dow = schedule.cadence.cron.split()
            # Bind the tzinfo once so RedBeat's crontab computes its next fire in the
            # schedule's timezone (DST-correct, agreeing with our ``compute_next_run``).
            tzinfo = ZoneInfo(schedule.timezone)
            entry = RedBeatSchedulerEntry(
                name=_entry_name(schedule.id),
                task="lumen.fire_schedule",
                schedule=crontab(
                    minute=minute,
                    hour=hour,
                    day_of_month=dom,
                    month_of_year=month,
                    day_of_week=dow,
                    nowfun=lambda tz=tzinfo: datetime.now(tz),
                ),
                args=[str(schedule.id), str(schedule.tenant_id)],
                app=celery_app,
            )
            entry.save()
        except Exception as exc:  # noqa: BLE001 — best-effort; Postgres is authoritative
            log.warning(
                "schedule.project_failed",
                schedule_id=str(schedule.id),
                error=type(exc).__name__,
            )

    def remove(self, schedule_id: UUID) -> None:
        try:
            from redbeat import RedBeatSchedulerEntry

            entry = RedBeatSchedulerEntry.from_key(
                RedBeatSchedulerEntry(name=_entry_name(schedule_id), app=celery_app).key,
                app=celery_app,
            )
            entry.delete()
        except Exception as exc:  # noqa: BLE001 — best-effort; a missing entry is fine
            log.debug(
                "schedule.unproject_noop",
                schedule_id=str(schedule_id),
                error=type(exc).__name__,
            )


# --- Overlap / concurrency / rate gates (ADR-0015 §5) -----------------------


def _run_rate_limiter(settings: Settings) -> RateLimiter:
    """The per-tenant run-enqueue rate limiter (reuses the Redis fixed-window pattern)."""
    return RedisFixedWindowRateLimiter(
        settings.redis_url,
        max_per_window=settings.run_rate_max_per_window,
        window_seconds=settings.run_rate_window_seconds,
        key_prefix=_RUN_RATE_KEY_PREFIX,
    )


async def _dispatch_fire(
    schedule_id: UUID,
    tenant_id: UUID,
    *,
    settings: Settings,
    rate_limiter: RateLimiter | None = None,
) -> str:
    """Run the gates + enqueue a scheduled run, then record the fire (ADR-0015 §4/§5).

    Runs under ``tenant_session_scope`` (as the schedule's tenant, INV-1). Steps:

    1. Load the schedule; a disabled/deleted schedule is a no-op (``no_schedule``).
    2. **Overlap gate** (default ``skip``): if a prior run for this schedule is still
       ``queued``/``running`` and the policy is ``skip``, skip this fire — advance
       ``next_run_at`` and enqueue nothing (a slow daily run never stacks up).
       ``queue``/``allow`` proceed.
    3. **Per-tenant concurrency + rate caps**: too many in-flight runs, or over the
       per-window rate, **defers** this fire (advance next_run_at, enqueue nothing) —
       never a silent drop; the schedule keeps ticking.
    4. **Enqueue** a ``schedule``-triggered headless run (``enqueue_manual_run`` pins
       the assistant's head version + snapshots the schedule's ``input_params``; a
       disabled/unpublished assistant is rejected there), then enqueue the Celery task
       after the row commits (after-commit, like run-now).
    5. Record ``last_run_at``/``last_status=queued`` + the recomputed tz/DST-correct
       ``next_run_at``.

    Returns a short outcome string (``enqueued`` / ``skipped_overlap`` / ``deferred_*``
    / ``no_schedule`` / ``assistant_unavailable``) for the task's structured log.
    """
    from app.domain.entities import RunTrigger
    from app.services.runs_service import enqueue_manual_run

    limiter = rate_limiter or _run_rate_limiter(settings)
    now = datetime.now(UTC)
    run_id: UUID | None = None

    async with tenant_session_scope(tenant_id) as session:
        schedules = ScheduleRepository(session, tenant_id)
        schedule = await schedules.get(schedule_id)
        if schedule is None or not schedule.enabled:
            return "no_schedule"

        runs = RunRepository(session, tenant_id)
        active = await runs.count_active_for_schedule(schedule.id)
        next_at = _safe_next_run(schedule)

        # Overlap gate (ADR-0015 §5) — default skip when a prior run is still active.
        if active > 0 and schedule.overlap_policy is OverlapPolicy.SKIP:
            await schedules.advance_next_run(schedule.id, next_run_at=next_at)
            return "skipped_overlap"

        # Per-tenant concurrency cap (ADR-0015 §5).
        if active >= settings.run_max_in_flight_per_tenant:
            await schedules.advance_next_run(schedule.id, next_run_at=next_at)
            return "deferred_concurrency"

        # Per-tenant rate cap (Redis fixed window; fail-open on a Redis outage).
        if not limiter.try_acquire(tenant_id):
            await schedules.advance_next_run(schedule.id, next_run_at=next_at)
            return "deferred_rate"

        # Enqueue a schedule-triggered run (published-assistant + head-version checks
        # are inside enqueue_manual_run; a disabled/unpublished assistant is rejected).
        try:
            run = await enqueue_manual_run(
                session,
                tenant_id=tenant_id,
                owner_id=schedule.owner_id,
                assistant_id=schedule.assistant_id,
                inputs=schedule.input_params,
                trigger=RunTrigger.SCHEDULE,
                schedule_id=schedule.id,
            )
        except Exception as exc:  # noqa: BLE001 — a bad assistant must not stall the schedule
            await schedules.record_fire(
                schedule.id, last_run_at=now, last_status=RunStatus.FAILED, next_run_at=next_at
            )
            log.warning(
                "schedule.fire_assistant_unavailable",
                schedule_id=str(schedule.id),
                error=type(exc).__name__,
            )
            return "assistant_unavailable"

        await schedules.record_fire(
            schedule.id, last_run_at=now, last_status=RunStatus.QUEUED, next_run_at=next_at
        )
        run_id = run.id

    # After the run row + fire summary commit, enqueue the Celery task (mirrors the
    # run-now router: after-commit so a broker outage never rolls back the run row).
    if run_id is not None:
        from app.tasks.run_assistant import enqueue_run

        enqueue_run(run_id, tenant_id)
    return "enqueued"


def _safe_next_run(schedule: Schedule) -> datetime | None:
    """Recompute the schedule's next fire, tolerating a pathological cadence."""
    try:
        return compute_next_run(schedule.cadence, schedule.timezone)
    except Exception:  # noqa: BLE001 — never let a bad cron/tz stall the fire loop
        return None


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.fire_schedule",
    bind=True,
    acks_late=True,
    max_retries=3,
    default_retry_delay=30,
)
def fire_schedule(self: object, schedule_id: str, tenant_id: str) -> dict[str, object]:
    """Celery entrypoint RedBeat fires: dispatch one scheduled run (the sync wrapper).

    Drives :func:`_dispatch_fire` on a fresh event loop via ``run_task`` (engine
    disposed per run, #140). A **transient** failure (Redis/DB briefly unavailable)
    retries with backoff up to the cap — a run is never silently dropped (ADR-0015
    §5). On retry exhaustion the fire is logged and acknowledged (the schedule keeps
    firing on its cadence; a single missed fire is not a stuck row).
    """
    sid = UUID(schedule_id)
    tid = UUID(tenant_id)
    settings = get_settings()
    try:
        outcome = run_task(_dispatch_fire(sid, tid, settings=settings))
    except Exception as exc:  # noqa: BLE001 — retry transient, then give up gracefully
        log.warning(
            "fire_schedule.error",
            schedule_id=schedule_id,
            error_type=type(exc).__name__,
        )
        try:
            raise self.retry(exc=exc)  # type: ignore[attr-defined]
        except self.MaxRetriesExceededError:  # type: ignore[attr-defined]
            log.error("fire_schedule.retries_exhausted", schedule_id=schedule_id)
            return {"schedule_id": schedule_id, "outcome": "retries_exhausted"}
    return {"schedule_id": schedule_id, "outcome": outcome}


# --- Beat config + boot reconcile -------------------------------------------


def configure_beat(settings: Settings | None = None) -> None:
    """Apply the RedBeat scheduler settings to the Celery app (called by the beat entrypoint).

    Sets the RedBeat scheduler class, the key prefix, and the leader-lock timeout on
    the celery conf so ``celery -A app.tasks.celery_app beat --scheduler
    redbeat.RedBeatScheduler`` reads them. Idempotent.
    """
    settings = settings or get_settings()
    celery_app.conf.update(
        redbeat_redis_url=settings.redis_url,
        redbeat_key_prefix=settings.redbeat_key_prefix,
        redbeat_lock_timeout=settings.redbeat_lock_timeout_seconds,
        beat_scheduler="redbeat.RedBeatScheduler",
    )


async def _reconcile_from_postgres(settings: Settings) -> int:
    """Rebuild the RedBeat entries from every enabled schedule (boot / lost-Redis-entry).

    Postgres is the source of truth (ADR-0015 §1): on Beat boot (or after a Redis
    flush) the derived entries are re-derived from the ``schedules`` table so a lost
    entry is rebuilt. Cross-tenant read under a **bypass**-scoped session (the one
    schedule read that spans tenants — a deliberate system path, never a request).
    Returns the number of entries reprojected.
    """
    projector = RedBeatScheduleProjector(settings)
    count = 0
    async with session_scope() as session:
        await bind_bypass(session)
        for schedule in await ScheduleReconcileRepository(session).list_enabled():
            projector.sync(schedule)
            count += 1
    return count


def reconcile_from_postgres(settings: Settings | None = None) -> int:
    """Sync entrypoint for the boot reconcile (drives the async core on a fresh loop)."""
    settings = settings or get_settings()
    return run_task(_reconcile_from_postgres(settings))


__all__ = [
    "NullScheduleProjector",
    "RedBeatScheduleProjector",
    "ScheduleProjector",
    "configure_beat",
    "fire_schedule",
    "reconcile_from_postgres",
]
