"""Schedule CRUD + pause/resume/run-now use-cases (ADR-0015, issue #236).

The orchestration layer for the ``/schedules`` surface (ADR-0004: ``services/``
compose adapters; routers call exactly one service). It pairs the tenant-scoped
``db/`` :class:`~app.db.repositories.ScheduleRepository` (the only SQL) with the
pure ``domain.scheduling`` cadence/timezone validation + DST-correct next-run
computation, the :class:`~app.tasks.scheduler.ScheduleProjector` seam (reconciles
the derived RedBeat entry), and the one audit sink (INV-6).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).** Every
operation is scoped to the caller's tenant (the repository) *and* the caller's
ownership (this service). A schedule in another tenant — or owned by another user
(and the caller is not a tenant admin) — is treated as **non-existent**: 404
(existence non-disclosure, never 403). The ``tenant_id``/``owner_id`` come from the
resolved principal, never request input.

**Validation (INV-8, fail-closed).** A malformed cron → **422** ``invalid_cron``; an
unknown IANA timezone → **422** ``invalid_timezone``. An unknown/cross-tenant/non-owned
assistant → **404**. A schedule may only run an assistant the owner can run: the
assistant must be **published** (a disabled/draft assistant → 422 at create, mirrored
in the run-now path); ``run-now`` on a **paused** schedule → **409** ``schedule_paused``
(INV-8, illegal transition).

**Runner-as-principal (INV-2).** The schedule's ``owner_id`` is the fired run's
principal, so a headless run retrieves only what the owner could retrieve
interactively — the load-bearing security property (enforced in ``retrieval/`` at run
time). This service only records *who owns* the schedule; it never widens access.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.repositories import (
    AssistantRepository,
    AssistantVersionRepository,
    RunRepository,
    ScheduleRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    Assistant,
    AssistantStatus,
    AuditOutcome,
    OverlapPolicy,
    Role,
    Run,
    RunTrigger,
    Schedule,
    ScheduleDelivery,
)
from app.domain.scheduling import (
    Cadence,
    InvalidCronError,
    InvalidTimezoneError,
    compute_next_run,
    validate_timezone,
)
from app.services.audit import AuditSink, PermissionDeniedRecorder
from app.services.runs_service import enqueue_manual_run
from app.tasks.scheduler import NullScheduleProjector, ScheduleProjector

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# The sentinel the API layer uses to distinguish "field omitted" from "field set to
# null/value" on a PATCH (mirrors the assistants service).
UNSET: object = object()

_SCHEDULE_CURSOR_PREFIX = "sch:"


def _encode_cursor(row_id: UUID) -> str:
    raw = f"{_SCHEDULE_CURSOR_PREFIX}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_SCHEDULE_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_SCHEDULE_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


@dataclass(frozen=True, slots=True)
class SchedulePage:
    """One page of schedules (newest first) + the opaque cursor for the next page."""

    items: list[Schedule]
    next_cursor: str | None


def _validate_cadence(cadence: Cadence, timezone: str) -> None:
    """Re-validate a normalized cadence + timezone before persisting (fail-closed, INV-8).

    The router already normalized the wire ``Cadence`` (cron OR structured) into a
    canonical :class:`~app.domain.scheduling.Cadence` (raising on a malformed cron);
    this re-runs the timezone check + a next-run computation so an unknown IANA name
    or a cron that never fires is a **422** here, never a stored schedule that can
    never produce a ``next_run_at``.
    """
    try:
        validate_timezone(timezone)
        compute_next_run(cadence, timezone)
    except InvalidCronError as exc:
        raise ValidationError(str(exc), code="invalid_cron") from exc
    except InvalidTimezoneError as exc:
        raise ValidationError(str(exc), code="invalid_timezone") from exc


class SchedulesService:
    """Create / read / update / delete / pause / resume / run-now schedules.

    Constructed per-request with the session, the resolved ``tenant_id`` /
    ``owner_id`` + the caller's ``roles`` (all from the token, never request input),
    the audit sink + correlation context, and the RedBeat projector seam (a no-op
    projector by default — Postgres is authoritative; the Beat boot reconcile derives
    entries from the table). All ownership/tenancy enforcement lives here; the
    repository enforces tenancy (INV-1) and this service enforces the owner-or-admin
    rule and the validation invariants.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        roles: tuple[Role, ...],
        audit: AuditSink,
        denials: PermissionDeniedRecorder,
        request_id: str,
        source_ip: str,
        projector: ScheduleProjector | None = None,
    ) -> None:
        self._session = session
        self._schedules = ScheduleRepository(session, tenant_id)
        self._assistants = AssistantRepository(session, tenant_id)
        self._versions = AssistantVersionRepository(session, tenant_id)
        self._runs = RunRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._is_admin = Role.ADMIN in roles
        self._audit = audit
        self._denials = denials
        self._request_id = request_id
        self._source_ip = source_ip
        self._projector = projector or NullScheduleProjector()

    # --- authorization ------------------------------------------------------

    def _may_manage(self, schedule: Schedule) -> bool:
        """Whether the caller may edit/pause/delete ``schedule`` (owner or tenant admin)."""
        return self._is_admin or schedule.owner_id == self._owner_id

    async def _load_managed_or_404(self, schedule_id: UUID, *, attempted_action: str) -> Schedule:
        """Load a schedule the caller may manage, or raise 404 (existence non-disclosure).

        Deny-by-default (INV-1/INV-2): the schedule must exist in this tenant **and**
        be owned by the caller (or the caller must be a tenant admin). A missing,
        cross-tenant, or non-owned id is a 404 — a non-owner is indistinguishable from
        a non-existent id, so the surface never reveals a foreign/unowned schedule.
        """
        schedule = await self._schedules.get(schedule_id)
        if schedule is None or not self._may_manage(schedule):
            await self._denials.emit(
                actor_id=self._owner_id,
                resource_type="schedule",
                resource_id=str(schedule_id),
                attempted_action=attempted_action,
                reason="not_visible",
                request_id=self._request_id,
                source_ip=self._source_ip,
            )
            raise NotFoundError("Schedule not found.")
        return schedule

    async def _load_runnable_assistant_or_error(
        self, assistant_id: UUID, *, attempted_action: str
    ) -> Assistant:
        """Load a published, owner-visible assistant, or raise (404 unknown / 422 not runnable).

        A schedule may only run an assistant the owner can run: unknown /
        cross-tenant / non-owned → **404** (INV-1/INV-2); a **draft or disabled**
        assistant → **422** ``assistant_not_runnable`` (a disabled/unauthorized
        assistant cannot be scheduled — the mandatory negative, ADR-0015 §5 / #217).
        Requires a pinned published head version too (else it could not run
        reproducibly).
        """
        assistant = await self._assistants.get(assistant_id)
        if assistant is None or (not self._is_admin and assistant.owner_id != self._owner_id):
            await self._denials.emit(
                actor_id=self._owner_id,
                resource_type="assistant",
                resource_id=str(assistant_id),
                attempted_action=attempted_action,
                reason="not_visible",
                request_id=self._request_id,
                source_ip=self._source_ip,
            )
            raise NotFoundError("Assistant not found.")
        if assistant.status is not AssistantStatus.PUBLISHED:
            raise ValidationError(
                "Only a published assistant can be scheduled.",
                code="assistant_not_runnable",
            )
        head = await self._versions.get_head(assistant.id)
        if head is None:
            raise ValidationError(
                "The assistant has no published version to run.",
                code="assistant_not_runnable",
            )
        return assistant

    # --- audit --------------------------------------------------------------

    async def _emit_audit(
        self,
        *,
        action: AuditAction,
        schedule_id: UUID,
        metadata: dict[str, object],
    ) -> None:
        await self._audit.emit(
            action=action,
            actor=AuditActor.user(self._owner_id),
            resource_type="schedule",
            resource_id=str(schedule_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata=metadata,
        )

    # --- CRUD use-cases -----------------------------------------------------

    async def create(
        self,
        *,
        assistant_id: UUID,
        cadence: Cadence,
        timezone: str,
        input_params: dict[str, object] | None = None,
        delivery: ScheduleDelivery | None = None,
        overlap_policy: OverlapPolicy = OverlapPolicy.SKIP,
        enabled: bool = True,
    ) -> Schedule:
        """Create a schedule owned by the caller (ADR-0015 §8, owner-gated T1 write).

        Validates the assistant (owned + published + versioned, else 404/422), the
        cadence + timezone (422 on a bad cron/tz), computes the DST-correct
        ``next_run_at`` (null when created paused), persists, projects the RedBeat
        entry (best-effort), and audits ``schedule.created`` (INV-6).
        """
        await self._load_runnable_assistant_or_error(
            assistant_id, attempted_action="schedule.create"
        )
        _validate_cadence(cadence, timezone)
        next_run_at = compute_next_run(cadence, timezone) if enabled else None
        schedule = await self._schedules.create(
            owner_id=self._owner_id,
            assistant_id=assistant_id,
            cadence=cadence,
            timezone=timezone,
            input_params=input_params,
            delivery=delivery,
            overlap_policy=overlap_policy,
            enabled=enabled,
            next_run_at=next_run_at,
        )
        self._projector.sync(schedule)
        await self._emit_audit(
            action=AuditAction.SCHEDULE_CREATED,
            schedule_id=schedule.id,
            metadata={
                "assistant_id": str(assistant_id),
                "timezone": timezone,
                "enabled": enabled,
                "overlap_policy": overlap_policy.value,
            },
        )
        return schedule

    async def get(self, schedule_id: UUID) -> Schedule:
        """Fetch one schedule the caller may see, or 404 (INV-1/INV-2)."""
        return await self._load_managed_or_404(schedule_id, attempted_action="schedule.read")

    async def list_(
        self,
        *,
        cursor: str | None,
        limit: int | None,
        assistant_id: UUID | None = None,
        enabled: bool | None = None,
    ) -> SchedulePage:
        """A keyset page of the caller's own schedules (newest first), filterable."""
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._schedules.list_for_owner_page(
            self._owner_id,
            limit=page_size + 1,
            after_id=after_id,
            assistant_id=assistant_id,
            enabled=enabled,
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return SchedulePage(items=page, next_cursor=next_cursor)

    async def update(
        self,
        schedule_id: UUID,
        *,
        cadence: object = UNSET,
        timezone: object = UNSET,
        input_params: object = UNSET,
        delivery: object = UNSET,
        overlap_policy: object = UNSET,
        enabled: object = UNSET,
    ) -> Schedule:
        """Patch a schedule; not visible/owned → 404, invalid cron/tz → 422 (ADR-0015 §8).

        Only the fields the caller passed (not ``UNSET``) are touched. Editing the
        cadence or timezone re-validates and recomputes ``next_run_at``; toggling
        ``enabled`` here is equivalent to pause/resume (next_run_at is nulled when
        disabled, recomputed when re-enabled). Re-projects the RedBeat entry and
        audits ``schedule.updated``.
        """
        existing = await self._load_managed_or_404(schedule_id, attempted_action="schedule.update")

        new_cadence = existing.cadence
        new_timezone = existing.timezone
        cadence_arg: Cadence | None = None
        timezone_arg: str | None = None
        if cadence is not UNSET:
            assert isinstance(cadence, Cadence)
            new_cadence = cadence
            cadence_arg = cadence
        if timezone is not UNSET:
            assert isinstance(timezone, str)
            new_timezone = timezone
            timezone_arg = timezone

        # Re-validate whenever either axis changed (a new cron under an old tz, or a
        # new tz under an old cron, must both still produce a fire time).
        if cadence is not UNSET or timezone is not UNSET:
            _validate_cadence(new_cadence, new_timezone)

        new_enabled = existing.enabled
        enabled_arg: bool | None = None
        if enabled is not UNSET:
            assert isinstance(enabled, bool)
            new_enabled = enabled
            enabled_arg = enabled

        input_arg: dict[str, object] | None = None
        if input_params is not UNSET:
            assert isinstance(input_params, dict)
            input_arg = input_params
        delivery_arg: ScheduleDelivery | None = None
        if delivery is not UNSET:
            assert isinstance(delivery, ScheduleDelivery)
            delivery_arg = delivery
        overlap_arg: OverlapPolicy | None = None
        if overlap_policy is not UNSET:
            assert isinstance(overlap_policy, OverlapPolicy)
            overlap_arg = overlap_policy

        # Recompute next_run_at whenever cadence/tz/enabled changed.
        next_run_arg: datetime | None = None
        clear_next = False
        if cadence is not UNSET or timezone is not UNSET or enabled is not UNSET:
            if new_enabled:
                next_run_arg = compute_next_run(new_cadence, new_timezone)
            else:
                clear_next = True

        updated = await self._schedules.update(
            schedule_id,
            cadence=cadence_arg,
            timezone=timezone_arg,
            input_params=input_arg,
            delivery=delivery_arg,
            overlap_policy=overlap_arg,
            enabled=enabled_arg,
            next_run_at=next_run_arg,
            clear_next_run_at=clear_next,
        )
        if updated is None:  # pragma: no cover — visibility already established
            raise NotFoundError("Schedule not found.")
        self._projector.sync(updated)
        await self._emit_audit(
            action=AuditAction.SCHEDULE_UPDATED,
            schedule_id=schedule_id,
            metadata={
                "fields": sorted(
                    self._changed_fields(
                        cadence, timezone, input_params, delivery, overlap_policy, enabled
                    )
                )
            },
        )
        return updated

    @staticmethod
    def _changed_fields(*args: object) -> list[str]:
        names = ["cadence", "timezone", "input_params", "delivery", "overlap_policy", "enabled"]
        return [name for name, value in zip(names, args, strict=True) if value is not UNSET]

    async def delete(self, schedule_id: UUID) -> None:
        """Delete a schedule the caller owns (removes its RedBeat entry), or 404."""
        schedule = await self._load_managed_or_404(schedule_id, attempted_action="schedule.delete")
        await self._schedules.delete(schedule.id)
        self._projector.remove(schedule.id)
        await self._emit_audit(
            action=AuditAction.SCHEDULE_DELETED,
            schedule_id=schedule.id,
            metadata={"assistant_id": str(schedule.assistant_id)},
        )

    # --- controls: pause / resume / run-now ---------------------------------

    async def pause(self, schedule_id: UUID) -> Schedule:
        """Pause a schedule (enabled=false, remove its RedBeat entry). Idempotent (ADR-0015 §6).

        Pausing an already-paused schedule returns it unchanged (200). Not
        visible/owned → 404. Audited ``schedule.paused``.
        """
        schedule = await self._load_managed_or_404(schedule_id, attempted_action="schedule.pause")
        if not schedule.enabled:
            return schedule  # idempotent — already paused
        updated = await self._schedules.update(schedule.id, enabled=False, clear_next_run_at=True)
        assert updated is not None
        self._projector.remove(schedule.id)
        await self._emit_audit(
            action=AuditAction.SCHEDULE_PAUSED, schedule_id=schedule.id, metadata={}
        )
        return updated

    async def resume(self, schedule_id: UUID) -> Schedule:
        """Resume a schedule (enabled=true, re-derive entry + next_run_at); idempotent (§6).

        Resuming an already-enabled schedule returns it unchanged (200). Not
        visible/owned → 404. Recomputes ``next_run_at`` tz/DST-correct. Audited
        ``schedule.resumed``.
        """
        schedule = await self._load_managed_or_404(schedule_id, attempted_action="schedule.resume")
        if schedule.enabled:
            return schedule  # idempotent — already firing
        next_run_at = compute_next_run(schedule.cadence, schedule.timezone)
        updated = await self._schedules.update(schedule.id, enabled=True, next_run_at=next_run_at)
        assert updated is not None
        self._projector.sync(updated)
        await self._emit_audit(
            action=AuditAction.SCHEDULE_RESUMED, schedule_id=schedule.id, metadata={}
        )
        return updated

    async def run_now(self, schedule_id: UUID) -> Run:
        """Enqueue an out-of-band ``manual`` run of the schedule's assistant now (ADR-0015 §6).

        Owner-gated (404 if not visible). A **paused** schedule → **409**
        ``schedule_paused`` (INV-8, illegal transition — a paused schedule does not
        run, even on demand). Re-checks the assistant is still runnable (published +
        versioned; a since-disabled assistant → 422). Creates a ``queued`` manual run
        pinned to the head version (the caller enqueues the Celery task after-commit),
        and audits ``schedule.run_now``. Returns the created run (the caller returns
        its id — the WS ``streamId`` for live attach).
        """
        schedule = await self._load_managed_or_404(schedule_id, attempted_action="schedule.run_now")
        if not schedule.enabled:
            raise ConflictError(
                "Cannot run a paused schedule; resume it first.",
                code="schedule_paused",
            )
        # Re-validate the assistant is still runnable (it may have been disabled).
        await self._load_runnable_assistant_or_error(
            schedule.assistant_id, attempted_action="schedule.run_now"
        )
        run = await enqueue_manual_run(
            self._session,
            tenant_id=self._tenant_id,
            owner_id=schedule.owner_id,
            assistant_id=schedule.assistant_id,
            denials=self._denials,
            request_id=self._request_id,
            source_ip=self._source_ip,
            inputs=schedule.input_params,
            trigger=RunTrigger.MANUAL,
            schedule_id=schedule.id,
        )
        await self._emit_audit(
            action=AuditAction.SCHEDULE_RUN_NOW,
            schedule_id=schedule.id,
            metadata={"run_id": str(run.id), "assistant_id": str(schedule.assistant_id)},
        )
        return run


__all__ = ["UNSET", "SchedulePage", "SchedulesService"]
