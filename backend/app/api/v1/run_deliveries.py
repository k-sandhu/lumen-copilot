"""Run-deliveries routes — the in-app run inbox (list) + mark-read (issue #238).

Contract-first: shapes match the **frozen** ``contracts/openapi.yaml``
``/run-deliveries*`` surface (``GET /run-deliveries`` → ``RunDeliveryList``;
``POST /run-deliveries/{deliveryId}/read`` → ``RunDelivery``). This module exposes a
module-level ``router`` and is **auto-discovered** (``api/v1/__init__.py`` scans for
it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all orchestration
(recipient/tenancy enforcement, the cursor codec, mark-read + its audit) lives in
``services.run_delivery_service``; this layer only (de)serialises + threads the
correlation context.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A cross-tenant / non-owned
delivery id is reported as **404** (existence non-disclosure): the service raises
``NotFoundError``, mapped by the global handler. Deliveries are produced by the run
task on completion (:mod:`app.services.runs_service`) / the digest beat, never by
these routes. **External channels (email/Slack) are T2-ish egress and OUT of scope**
(INV-7): this surface is in-app only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    extract_request_id,
)
from app.domain.entities import RunDelivery, RunDeliveryKind, RunDeliveryStatus
from app.services.run_delivery_service import RunDeliveryPage, RunDeliveryService

router = APIRouter(prefix="/run-deliveries", tags=["run-deliveries"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class RunDeliveryResponse(BaseModel):
    """``#/components/schemas/RunDelivery`` — one in-app delivery of a completed run."""

    model_config = {"extra": "forbid"}

    id: UUID
    run_id: UUID
    schedule_id: UUID | None = None
    kind: RunDeliveryKind
    status: RunDeliveryStatus
    summary: str | None = None
    created_at: datetime
    read_at: datetime | None = None


class RunDeliveryListResponse(BaseModel):
    """``#/components/schemas/RunDeliveryList``."""

    model_config = {"extra": "forbid"}

    items: list[RunDeliveryResponse]
    next_cursor: str | None = None


# --- Serialisation helpers --------------------------------------------------


def _to_response(delivery: RunDelivery) -> RunDeliveryResponse:
    return RunDeliveryResponse(
        id=delivery.id,
        run_id=delivery.run_id,
        schedule_id=delivery.schedule_id,
        kind=delivery.kind,
        status=delivery.status,
        summary=delivery.summary,
        created_at=delivery.created_at,
        read_at=delivery.read_at,
    )


def _list_to_response(page: RunDeliveryPage) -> RunDeliveryListResponse:
    return RunDeliveryListResponse(
        items=[_to_response(d) for d in page.items],
        next_cursor=page.next_cursor,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    request: Request,
) -> RunDeliveryService:
    return RunDeliveryService(
        session,
        tenant_id=tenant_id,
        recipient_id=principal.user_id,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=RunDeliveryListResponse, response_model_exclude_none=True)
async def list_run_deliveries(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    status_filter: Annotated[RunDeliveryStatus | None, Query(alias="status")] = None,
    unread: Annotated[bool, Query()] = False,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RunDeliveryListResponse:
    """The run inbox — list the caller's deliveries (newest first), filterable (#238)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    page = await service.list_(
        cursor=cursor,
        limit=limit,
        status=status_filter,
        unread_only=unread,
    )
    return _list_to_response(page)


@router.post(
    "/{delivery_id}/read",
    response_model=RunDeliveryResponse,
    response_model_exclude_none=True,
)
async def mark_run_delivery_read(
    delivery_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> RunDeliveryResponse:
    """Mark one delivery read (idempotent); not visible/owned → 404 (INV-1/INV-2)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    updated = await service.mark_read(delivery_id)
    await session.commit()
    return _to_response(updated)
