"""Collections routes — list / create / get / patch / delete (#46).

Contract-first: shapes match ``contracts/openapi.yaml`` (``CollectionCreate`` /
``CollectionUpdate`` → ``Collection``; ``CollectionList`` with cursor
pagination). Routers validate in → call **one** service → shape out (ADR-0004):
all orchestration (ownership/tenancy enforcement, document counts, audit, the
cursor codec) lives in ``services.collections_service``; this layer only
(de)serialises and threads correlation context.

**Tenancy + ownership (spec 0004 §2.1/§2.2).** The collection's ``tenant_id`` and
``owner_id`` come from the resolved principal (``current_user`` / ``current_tenant``
— the token, never request input). A collection in another tenant or owned by
another user is reported as **404** (existence non-disclosure, INV-1/INV-2): the
service returns ``None``/``False`` and this router maps that to ``NotFoundError``,
never 403. Every route requires the bearer token (contract ``bearerAuth``);
unauthenticated → 401 via ``current_user`` (INV-4).
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
from app.core.errors import NotFoundError
from app.services.collections_service import (
    CollectionPage,
    CollectionsService,
    CollectionView,
)

router = APIRouter(prefix="/collections", tags=["collections"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class CollectionCreate(BaseModel):
    """``#/components/schemas/CollectionCreate`` — name (+ optional description)."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class CollectionUpdate(BaseModel):
    """``#/components/schemas/CollectionUpdate`` — partial; at least one field.

    ``minProperties: 1`` in the contract: an empty body is rejected (422). We
    track which fields were actually supplied (``model_fields_set``) so the
    service can tell "description omitted" (leave as-is) from "description set to
    null" (clear it).
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> CollectionUpdate:
        """Enforce the contract's ``minProperties: 1`` (INV-8 → 422).

        An empty patch is meaningless and the contract forbids it; rejecting it
        here (rather than no-op'ing) keeps the boundary honest.
        """
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        return self


class CollectionResponse(BaseModel):
    """``#/components/schemas/Collection`` — the wire projection of a collection."""

    model_config = {"extra": "forbid"}

    id: UUID
    name: str
    description: str | None = None
    owner_id: UUID
    document_count: int
    created_at: datetime
    updated_at: datetime


class CollectionListResponse(BaseModel):
    """``#/components/schemas/CollectionList`` — a cursor-paginated page."""

    model_config = {"extra": "forbid"}

    items: list[CollectionResponse]
    next_cursor: str | None = None


# --- Serialisation helpers --------------------------------------------------


def _to_response(view: CollectionView) -> CollectionResponse:
    c = view.collection
    return CollectionResponse(
        id=c.id,
        name=c.name,
        description=c.description,
        owner_id=c.owner_id,
        document_count=view.document_count,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _to_list_response(page: CollectionPage) -> CollectionListResponse:
    return CollectionListResponse(
        items=[_to_response(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    request: Request,
) -> CollectionsService:
    """Assemble the per-request service from the identity + audit seams.

    The audit sink is bound to the caller's tenant (the factory closes over this
    request's session); ``request_id``/``source_ip`` thread the correlation
    context into audit envelopes without the service touching the request.
    """
    return CollectionsService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        audit=make_audit_sink(tenant_id),
        # The audit envelope requires a non-empty request_id / source_ip (spec
        # 0004 §2.4); fall back to a sentinel when the client supplied neither so
        # the create/delete write never fails closed on a missing correlation id.
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=CollectionListResponse, response_model_exclude_none=True)
async def list_collections(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CollectionListResponse:
    """List the caller's own collections (cursor-paginated, newest first)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    page = await service.list_page(cursor=cursor, limit=limit)
    return _to_list_response(page)


@router.post(
    "",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_collection(
    body: CollectionCreate,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> CollectionResponse:
    """Create a collection owned by the caller (audited: ``collection.created``)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    view = await service.create(name=body.name, description=body.description)
    await session.commit()
    return _to_response(view)


@router.get("/{collection_id}", response_model=CollectionResponse, response_model_exclude_none=True)
async def get_collection(
    collection_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> CollectionResponse:
    """Get one of the caller's collections; not visible → 404 (INV-1/INV-2)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    view = await service.get(collection_id)
    if view is None:
        raise NotFoundError("Collection not found.")
    return _to_response(view)


@router.patch(
    "/{collection_id}", response_model=CollectionResponse, response_model_exclude_none=True
)
async def update_collection(
    collection_id: UUID,
    body: CollectionUpdate,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> CollectionResponse:
    """Rename / re-describe one of the caller's collections; not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    view = await service.update(
        collection_id,
        name=body.name,
        description=body.description,
        set_description="description" in body.model_fields_set,
    )
    if view is None:
        raise NotFoundError("Collection not found.")
    await session.commit()
    return _to_response(view)


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    collection_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Delete one of the caller's collections (cascades); not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    deleted = await service.delete(collection_id)
    if not deleted:
        raise NotFoundError("Collection not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
