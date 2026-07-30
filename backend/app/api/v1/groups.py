"""Group administration routes — ``/admin/groups`` (ADR-0022 §2).

The HTTP surface over :class:`~app.services.groups_service.GroupsService`: an
admin creates groups and manages their membership, and a ``grants`` row naming a
group then admits every member (the retrieval widening lives in ``retrieval/``,
not here).

**Admin-only, structurally.** The gate is a *router-level* dependency —
``require_roles(Role.ADMIN)`` (spec 0004 §2.3) — so it runs on every path under
this module and a new route cannot forget it. The service asserts the same rule
again, so neither layer depends on the other being right (INV-5 → 403).

**Tenancy.** ``tenant_id`` comes from the token via ``CurrentTenant``, never from
request input (spec 0004 §2.3); declaring it also binds the Postgres RLS GUC for
the request session. A group — or a user — in another tenant is reported **404,
never 403** (existence non-disclosure, INV-1); the service raises
:class:`~app.core.errors.NotFoundError` and the app-level handler renders it.

The router holds no business logic (ADR-0004): every route builds the service,
delegates, maps the domain result onto a wire model, and commits. Errors are
raised as typed :class:`~app.core.errors.AppError` subclasses inside the service
and rendered as RFC-9457 ``application/problem+json`` by the global handler — no
route catches anything.

Mirrors ``contracts/openapi.yaml`` §``/admin/groups`` (ADR-0006, contract-first).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    extract_request_id,
    require_roles,
)
from app.domain.entities import Group, Role, User
from app.services.groups_service import MAX_GROUP_NAME_LENGTH, GroupsService

router = APIRouter(
    prefix="/admin/groups",
    tags=["admin"],
    dependencies=[Depends(require_roles(Role.ADMIN))],
)


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class GroupResponse(BaseModel):
    """``#/components/schemas/Group`` — one group.

    ``member_count`` is contract-**required** and nullable: ``null`` means the
    tenant's derived "All members" group, whose membership is not stored and
    therefore not countable (ADR-0022 §3). ``null`` is not zero, so this model
    must NOT be serialized with ``exclude_none`` — dropping the key would turn
    "everyone" into "field absent". It also carries **no default**: a default
    would make it optional in the generated schema, contradicting the
    contract's ``required``.
    """

    model_config = {"extra": "forbid"}

    id: UUID
    name: str
    kind: str
    member_count: int | None
    created_at: datetime
    updated_at: datetime


class GroupListResponse(BaseModel):
    """``#/components/schemas/GroupList`` — every group in the tenant."""

    model_config = {"extra": "forbid"}

    items: list[GroupResponse]


class GroupCreateRequest(BaseModel):
    """``#/components/schemas/GroupCreate`` — a new group's name."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=MAX_GROUP_NAME_LENGTH)


class GroupUpdateRequest(BaseModel):
    """``#/components/schemas/GroupUpdate`` — a group's new name."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=MAX_GROUP_NAME_LENGTH)


class GroupMemberAddRequest(BaseModel):
    """``#/components/schemas/GroupMemberAdd`` — the user to add."""

    model_config = {"extra": "forbid"}

    user_id: UUID


class GroupMemberResponse(BaseModel):
    """``#/components/schemas/Member`` — one member of a group.

    The same shape the admin roster uses, so the console can render a group's
    members with the fields it already knows. ``email_attested_at`` is
    contract-required and nullable, so this must not be serialized with
    ``exclude_none`` either, and carries no default for the same reason.
    """

    model_config = {"extra": "forbid"}

    id: UUID
    email: str
    role: list[str]
    email_attested_at: datetime | None


class GroupMemberListResponse(BaseModel):
    """``#/components/schemas/GroupMemberList`` — a group's explicit members."""

    model_config = {"extra": "forbid"}

    items: list[GroupMemberResponse]


def _to_response(group: Group, *, member_count: int | None) -> GroupResponse:
    """Map the domain group onto the wire shape."""
    return GroupResponse(
        id=group.id,
        name=group.name,
        kind=group.kind.value,
        member_count=member_count,
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


def _to_member_response(user: User) -> GroupMemberResponse:
    return GroupMemberResponse(
        id=user.id,
        email=user.email,
        role=[r.value for r in user.roles],
        email_attested_at=user.email_attested_at,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    request: Request,
) -> GroupsService:
    """Assemble the per-request service from the identity + audit seams."""
    return GroupsService(
        session,
        tenant_id=tenant_id,
        actor_id=principal.user_id,
        roles=principal.roles,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=GroupListResponse)
async def list_groups(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> GroupListResponse:
    """Every group in the tenant, system group first then by name."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    pairs = await service.list_groups_with_counts()
    # The listing is where the tenant's derived "All members" group is
    # materialized on first use (ADR-0022 §3 chose lazy creation over a
    # back-fill), so this read can create exactly one row and must commit it.
    await session.commit()
    return GroupListResponse(items=[_to_response(g, member_count=count) for g, count in pairs])


@router.post("", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> GroupResponse:
    """Create an empty group. Duplicate name → 409, blank/over-long → 422."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    group = await service.create_group(name=payload.name)
    await session.commit()
    return _to_response(group, member_count=0)


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> GroupResponse:
    """One group; a group in another tenant is 404, never 403 (INV-1)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    group = await service.get_group(group_id)
    return _to_response(group, member_count=await service.member_count(group_id))


@router.patch("/{group_id}", response_model=GroupResponse)
async def rename_group(
    group_id: UUID,
    payload: GroupUpdateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> GroupResponse:
    """Rename a group. The derived "All members" group is refused 409."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    group = await service.rename_group(group_id, name=payload.name)
    # Read the count BEFORE committing. ``current_tenant`` binds the RLS GUC
    # with ``SET LOCAL`` semantics, so it is reset when the transaction ends —
    # a read after ``commit()`` runs in a new, unbound transaction where
    # Postgres RLS admits nothing, turning a successful rename into a 404.
    # Invisible on the offline SQLite engine, which is why it is called out.
    member_count = await service.member_count(group_id)
    await session.commit()
    return _to_response(group, member_count=member_count)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Delete a group, its membership, and every grant naming it."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    await service.delete_group(group_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{group_id}/members", response_model=GroupMemberListResponse)
async def list_group_members(
    group_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> GroupMemberListResponse:
    """The group's explicit members, by email. Empty for the system group."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    members = await service.list_members(group_id)
    return GroupMemberListResponse(items=[_to_member_response(u) for u in members])


@router.post("/{group_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_group_member(
    group_id: UUID,
    payload: GroupMemberAddRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Add a user to a group (idempotent). A foreign-tenant user is 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    await service.add_member(group_id, user_id=payload.user_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{group_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_member(
    group_id: UUID,
    member_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Remove a user from a group (idempotent). Revokes on their next request."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    await service.remove_member(group_id, user_id=member_id)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
