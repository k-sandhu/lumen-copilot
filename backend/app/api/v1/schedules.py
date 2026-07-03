"""Schedules routes — CRUD + pause / resume / run-now (ADR-0015 §8, issue #236).

Contract-first: shapes match the **frozen** ``contracts/openapi.yaml`` ``/schedules*``
surface (``ScheduleCreate`` / ``ScheduleUpdate`` → ``Schedule``; ``ScheduleList``;
``pause`` / ``resume`` → ``Schedule``; ``run-now`` → ``RunEnqueued``, 202). This module
exposes a module-level ``router`` and is **auto-discovered** (``api/v1/__init__.py``
scans for it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all orchestration
(ownership/tenancy enforcement, cron/timezone validation, next-run computation, the
RedBeat projection, audit, the run-now enqueue, the cursor codec) lives in
``services.schedules_service``; this layer only (de)serialises + threads the
correlation context, normalizes the wire ``Cadence`` to the domain form, and enqueues
the run-now Celery task **after-commit**.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A cross-tenant / non-owned
schedule id is **404** (existence non-disclosure). A malformed cron / unknown IANA
timezone / a non-runnable assistant is **422** (INV-8); ``run-now`` on a paused
schedule is **409** (INV-8, illegal transition). All mapped by the global handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    extract_request_id,
)
from app.core.errors import ValidationError as DomainValidationError
from app.domain.entities import (
    DigestCadence,
    OverlapPolicy,
    RunStatus,
    Schedule,
    ScheduleDelivery,
)
from app.domain.scheduling import (
    Cadence,
    CadenceUnit,
    InvalidCronError,
    StructuredCadence,
    cadence_from_cron,
    cadence_from_structured,
)
from app.services.schedules_service import (
    UNSET,
    SchedulePage,
    SchedulesService,
)
from app.tasks.run_assistant import enqueue_run

router = APIRouter(prefix="/schedules", tags=["schedules"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class StructuredCadenceModel(BaseModel):
    """``#/components/schemas/StructuredCadence`` — the human-friendly recurrence."""

    model_config = {"extra": "forbid"}

    every: CadenceUnit
    at: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    day_of_week: int | None = Field(default=None, ge=0, le=6)
    day_of_month: int | None = Field(default=None, ge=1, le=31)

    def to_domain(self) -> StructuredCadence:
        return StructuredCadence(
            every=self.every,
            at=self.at,
            day_of_week=self.day_of_week,
            day_of_month=self.day_of_month,
        )

    @classmethod
    def from_domain(cls, sc: StructuredCadence) -> StructuredCadenceModel:
        return cls(
            every=sc.every,
            at=sc.at,
            day_of_week=sc.day_of_week,
            day_of_month=sc.day_of_month,
        )


class CadenceModel(BaseModel):
    """``#/components/schemas/Cadence`` — exactly one of ``cron`` OR ``structured``."""

    model_config = {"extra": "forbid"}

    cron: str | None = None
    structured: StructuredCadenceModel | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> CadenceModel:
        if (self.cron is None) == (self.structured is None):
            raise ValueError("Cadence must carry exactly one of `cron` or `structured`.")
        return self

    def to_domain(self) -> Cadence:
        """Normalize to the domain :class:`Cadence` (raises 422 on a malformed cron)."""
        try:
            if self.cron is not None:
                return cadence_from_cron(self.cron)
            assert self.structured is not None
            return cadence_from_structured(self.structured.to_domain())
        except InvalidCronError as exc:
            raise DomainValidationError(str(exc), code="invalid_cron") from exc

    @classmethod
    def from_domain(cls, cadence: Cadence) -> CadenceModel:
        # Round-trip the original structured form when the schedule was created that
        # way (the UI prefers the human form); else surface the stored cron.
        if cadence.structured is not None:
            return cls(structured=StructuredCadenceModel.from_domain(cadence.structured))
        return cls(cron=cadence.cron)


class ScheduleDeliveryModel(BaseModel):
    """``#/components/schemas/ScheduleDelivery`` — v1 in-app inbox + optional digest."""

    model_config = {"extra": "forbid"}

    inbox: bool = True
    digest: DigestCadence | None = None

    def to_domain(self) -> ScheduleDelivery:
        return ScheduleDelivery(inbox=self.inbox, digest=self.digest)

    @classmethod
    def from_domain(cls, delivery: ScheduleDelivery) -> ScheduleDeliveryModel:
        return cls(inbox=delivery.inbox, digest=delivery.digest)


class ScheduleCreateRequest(BaseModel):
    """``#/components/schemas/ScheduleCreate``."""

    model_config = {"extra": "forbid"}

    assistant_id: UUID
    cadence: CadenceModel
    timezone: str = Field(min_length=1)
    input_params: dict[str, object] | None = None
    delivery: ScheduleDeliveryModel | None = None
    overlap_policy: OverlapPolicy | None = None
    enabled: bool = True


class ScheduleUpdateRequest(BaseModel):
    """``#/components/schemas/ScheduleUpdate`` — at least one field."""

    model_config = {"extra": "forbid"}

    cadence: CadenceModel | None = None
    timezone: str | None = Field(default=None, min_length=1)
    input_params: dict[str, object] | None = None
    delivery: ScheduleDeliveryModel | None = None
    overlap_policy: OverlapPolicy | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ScheduleUpdateRequest:
        if not any(
            v is not None
            for v in (
                self.cadence,
                self.timezone,
                self.input_params,
                self.delivery,
                self.overlap_policy,
                self.enabled,
            )
        ):
            raise ValueError("A schedule update must change at least one field.")
        return self


class ScheduleResponse(BaseModel):
    """``#/components/schemas/Schedule`` — a schedule projection."""

    model_config = {"extra": "forbid"}

    id: UUID
    assistant_id: UUID
    owner_id: UUID
    cadence: CadenceModel
    timezone: str
    input_params: dict[str, object] | None = None
    delivery: ScheduleDeliveryModel
    overlap_policy: OverlapPolicy
    enabled: bool
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: RunStatus | None = None
    created_at: datetime
    updated_at: datetime


class ScheduleListResponse(BaseModel):
    """``#/components/schemas/ScheduleList``."""

    model_config = {"extra": "forbid"}

    items: list[ScheduleResponse]
    next_cursor: str | None = None


class RunEnqueuedResponse(BaseModel):
    """``#/components/schemas/RunEnqueued`` — the new run's id (also its WS streamId)."""

    model_config = {"extra": "forbid"}

    run_id: UUID


# --- Serialisation ----------------------------------------------------------


def _to_response(schedule: Schedule) -> ScheduleResponse:
    return ScheduleResponse(
        id=schedule.id,
        assistant_id=schedule.assistant_id,
        owner_id=schedule.owner_id,
        cadence=CadenceModel.from_domain(schedule.cadence),
        timezone=schedule.timezone,
        input_params=schedule.input_params or None,
        delivery=ScheduleDeliveryModel.from_domain(schedule.delivery),
        overlap_policy=schedule.overlap_policy,
        enabled=schedule.enabled,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        last_status=schedule.last_status,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


def _to_list_response(page: SchedulePage) -> ScheduleListResponse:
    return ScheduleListResponse(
        items=[_to_response(s) for s in page.items],
        next_cursor=page.next_cursor,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    request: Request,
) -> SchedulesService:
    """Assemble the per-request service from the identity + audit seams.

    The RedBeat projector is left as the service's default (a no-op): the derived
    Redis entry is reconciled from Postgres by the Beat process on boot, so a
    request path that cannot reach Redis never fails a CRUD write. A deploy that
    wants immediate projection wires the real ``RedBeatScheduleProjector`` here.
    """
    return SchedulesService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        roles=principal.roles,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=ScheduleListResponse, response_model_exclude_none=True)
async def list_schedules(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    assistant_id: Annotated[UUID | None, Query()] = None,
    enabled: Annotated[bool | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ScheduleListResponse:
    """List the caller's own schedules (cursor-paginated, newest first), filterable."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    page = await service.list_(
        cursor=cursor, limit=limit, assistant_id=assistant_id, enabled=enabled
    )
    return _to_list_response(page)


@router.post(
    "",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_schedule(
    body: ScheduleCreateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> ScheduleResponse:
    """Create a schedule (bad cron/tz → 422; unknown assistant → 404; disabled → 422)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    schedule = await service.create(
        assistant_id=body.assistant_id,
        cadence=body.cadence.to_domain(),
        timezone=body.timezone,
        input_params=body.input_params,
        delivery=body.delivery.to_domain() if body.delivery else None,
        overlap_policy=body.overlap_policy or OverlapPolicy.SKIP,
        enabled=body.enabled,
    )
    await session.commit()
    return _to_response(schedule)


@router.get("/{schedule_id}", response_model=ScheduleResponse, response_model_exclude_none=True)
async def get_schedule(
    schedule_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> ScheduleResponse:
    """Get one schedule; not visible/owned → 404 (INV-1/INV-2)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    return _to_response(await service.get(schedule_id))


@router.patch("/{schedule_id}", response_model=ScheduleResponse, response_model_exclude_none=True)
async def update_schedule(
    schedule_id: UUID,
    body: ScheduleUpdateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> ScheduleResponse:
    """Edit a schedule; not visible/owned → 404, bad cron/tz → 422 (INV-8)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    updated = await service.update(
        schedule_id,
        cadence=body.cadence.to_domain() if body.cadence is not None else UNSET,
        timezone=body.timezone if body.timezone is not None else UNSET,
        input_params=body.input_params if body.input_params is not None else UNSET,
        delivery=body.delivery.to_domain() if body.delivery is not None else UNSET,
        overlap_policy=body.overlap_policy if body.overlap_policy is not None else UNSET,
        enabled=body.enabled if body.enabled is not None else UNSET,
    )
    await session.commit()
    return _to_response(updated)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Delete a schedule (removes its RedBeat entry); not visible/owned → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    await service.delete(schedule_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{schedule_id}/pause", response_model=ScheduleResponse, response_model_exclude_none=True
)
async def pause_schedule(
    schedule_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> ScheduleResponse:
    """Pause a schedule (enabled=false). Idempotent; not visible/owned → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    paused = await service.pause(schedule_id)
    await session.commit()
    return _to_response(paused)


@router.post(
    "/{schedule_id}/resume", response_model=ScheduleResponse, response_model_exclude_none=True
)
async def resume_schedule(
    schedule_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> ScheduleResponse:
    """Resume a schedule (enabled=true, recompute next_run_at). Idempotent; not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    resumed = await service.resume(schedule_id)
    await session.commit()
    return _to_response(resumed)


@router.post(
    "/{schedule_id}/run-now",
    response_model=RunEnqueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_schedule_now(
    schedule_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> RunEnqueuedResponse:
    """Enqueue an out-of-band manual run now (202); paused schedule → 409 (INV-8).

    The run row + audit commit with the request; the Celery task is enqueued
    **after-commit** so a broker outage never turns a successful run-now into a 500
    nor rolls back the queued run (the run stays ``queued``; the pattern
    ``enqueue_ingestion``/run-now use).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    run = await service.run_now(schedule_id)
    await session.commit()
    enqueue_run(run.id, tenant_id)
    return RunEnqueuedResponse(run_id=run.id)
