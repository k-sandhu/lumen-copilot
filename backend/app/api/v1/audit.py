"""Audit-trail route — GET /audit (admin / security only) (#85, #80).

Contract-first: shapes match ``contracts/openapi.yaml`` (``AuditEvent`` /
``AuditProvenance`` / ``AuditCandidate`` → ``AuditEventList`` with cursor
pagination). The router validates in → calls **one** service
(:class:`~app.services.audit_query_service.AuditQueryService`) → shapes out
(ADR-0004): all query/filter/pagination/provenance logic lives in the service;
this layer only (de)serialises and applies the auth gates.

**Role gate (INV-5 → 403).** The audit trail is restricted to the ``admin`` and
``security`` roles; every other role (incl. ``member``) gets **403**, distinct
from the **401** an unauthenticated caller gets (INV-4). The gate is the shared
``require_roles`` dependency (``app.api.deps``) — role checks live in
``services/`` (spec 0004 §2.3), reused here, not re-implemented.

**Tenant scope (INV-1).** Events are scoped to the caller's tenant, resolved
from the token (``current_tenant``), never from request input — the service
applies the predicate.

The router is **auto-discovered** (ADR-0008 §3): exposing a module-level
``router`` is all that registers it; ``api/v1/__init__.py`` is never edited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import CurrentTenant, DbSession, require_roles
from app.domain.entities import Role
from app.services.audit_query_service import (
    AuditEventPage,
    AuditEventView,
    AuditQueryService,
)

# Reading the audit trail is an admin/security action (INV-5); a member → 403.
_audit_reader = require_roles(Role.ADMIN, Role.SECURITY)

router = APIRouter(prefix="/audit", tags=["audit"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class AuditCandidate(BaseModel):
    """``#/components/schemas/AuditCandidate`` — one allowed/excluded candidate."""

    model_config = {"extra": "forbid"}

    resource_id: str
    disposition: str
    reason: str
    score: float | None = None


class AuditProvenance(BaseModel):
    """``#/components/schemas/AuditProvenance`` — candidates + raw payload."""

    model_config = {"extra": "forbid"}

    candidates: list[AuditCandidate]
    raw: dict[str, object] | None = None


class AuditEvent(BaseModel):
    """``#/components/schemas/AuditEvent`` — one append-only audit event."""

    model_config = {"extra": "forbid"}

    id: UUID
    ts: datetime
    actor: str
    tenant_id: UUID
    event_type: str
    resource_id: str | None = None
    decision: str
    provenance: AuditProvenance


class AuditEventList(BaseModel):
    """``#/components/schemas/AuditEventList`` — a cursor-paginated page."""

    model_config = {"extra": "forbid"}

    items: list[AuditEvent]
    next_cursor: str | None = None


# --- Serialisation helpers --------------------------------------------------


def _to_event(view: AuditEventView) -> AuditEvent:
    return AuditEvent(
        id=view.id,
        ts=view.ts,
        actor=view.actor,
        tenant_id=view.tenant_id,
        event_type=view.event_type,
        resource_id=view.resource_id,
        decision=view.decision,
        provenance=AuditProvenance(
            candidates=[
                AuditCandidate(
                    resource_id=c.resource_id,
                    disposition=c.disposition,
                    reason=c.reason,
                    score=c.score,
                )
                for c in view.provenance.candidates
            ],
            raw=view.provenance.raw,
        ),
    )


def _to_list(page: AuditEventPage) -> AuditEventList:
    return AuditEventList(
        items=[_to_event(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


# --- Routes -----------------------------------------------------------------


@router.get(
    "",
    response_model=AuditEventList,
    response_model_exclude_none=True,
    dependencies=[Depends(_audit_reader)],
)
async def list_audit_events(
    session: DbSession,
    tenant_id: CurrentTenant,
    actor: Annotated[str | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    resource_id: Annotated[str | None, Query()] = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AuditEventList:
    """List the tenant's audit events (newest → oldest, cursor-paginated).

    Restricted to ``admin``/``security`` (INV-5 → 403 via ``require_roles``);
    tenant-scoped (INV-1). Filters: ``actor`` (user id, or ``system``/
    ``anonymous``), ``event_type`` (taxonomy action), ``resource_id``, and the
    ``[from, to)`` time window. Each event carries ``provenance`` (candidate
    allow/exclude dispositions + the raw recorded payload).
    """
    service = AuditQueryService(session, tenant_id=tenant_id)
    page = await service.query(
        actor=actor,
        event_type=event_type,
        resource_id=resource_id,
        from_ts=from_,
        to_ts=to,
        cursor=cursor,
        limit=limit,
    )
    return _to_list(page)
