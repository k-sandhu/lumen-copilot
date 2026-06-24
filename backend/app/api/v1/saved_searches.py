"""Saved-searches routes — CRUD over ``/saved-searches`` (spec 0005, epic #144).

Contract-first: shapes match ``contracts/openapi.yaml`` (``SavedSearchCreate`` /
``SavedSearchUpdate`` → ``SavedSearch``; ``SavedSearchList``). The router validates
in → calls **one** service → shapes out (ADR-0004); ownership/tenancy enforcement
and the cursor codec live in ``services.saved_searches_service``.

**Tenancy + ownership (spec 0004 §2.1/§2.2).** A saved search in another tenant or
owned by another user is reported as **404** (existence non-disclosure,
INV-1/INV-2): the service returns ``None``/``False`` and this router maps that to
``NotFoundError``, never 403. Every route requires the bearer token (INV-4); an
over-long ``name``/``query`` or a bad cursor is **422** (INV-8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import CurrentTenant, CurrentUser, DbSession
from app.core.errors import NotFoundError
from app.domain.entities import SavedSearch
from app.services.saved_searches_service import SavedSearchService

router = APIRouter(prefix="/saved-searches", tags=["saved-searches"])

# Mirrors contracts/openapi.yaml ResultSource (the /search source filter).
ResultSource = Literal["upload", "chat", "connector"]


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class SavedSearchCreate(BaseModel):
    """``#/components/schemas/SavedSearchCreate``."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=1000)
    collection_id: UUID | None = None
    source: ResultSource | None = None
    type: str | None = Field(default=None, max_length=100)


class SavedSearchUpdate(BaseModel):
    """``#/components/schemas/SavedSearchUpdate`` — partial; at least one field."""

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    collection_id: UUID | None = None
    source: ResultSource | None = None
    type: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _validate(self) -> SavedSearchUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        # name/query are non-nullable in the contract; an EXPLICIT null is malformed
        # (422), distinct from "absent" (unchanged). Only collection_id/source/type
        # are tri-state nullable (a null clears them).
        for field in ("name", "query"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} must not be null.")
        return self


class SavedSearchResponse(BaseModel):
    """``#/components/schemas/SavedSearch``."""

    model_config = {"extra": "forbid"}

    id: UUID
    name: str
    query: str
    collection_id: UUID | None = None
    source: str | None = None
    type: str | None = None
    owner_id: UUID
    created_at: datetime
    updated_at: datetime


class SavedSearchListResponse(BaseModel):
    """``#/components/schemas/SavedSearchList``."""

    model_config = {"extra": "forbid"}

    items: list[SavedSearchResponse]
    next_cursor: str | None = None


def _to_response(saved: SavedSearch) -> SavedSearchResponse:
    return SavedSearchResponse(
        id=saved.id,
        name=saved.name,
        query=saved.query,
        collection_id=saved.collection_id,
        source=saved.source,
        type=saved.type,
        owner_id=saved.owner_id,
        created_at=saved.created_at,
        updated_at=saved.updated_at,
    )


@router.get("", response_model=SavedSearchListResponse)
async def list_saved_searches(
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SavedSearchListResponse:
    """List the caller's saved searches (newest-updated first, cursor-paginated)."""
    service = SavedSearchService(session, tenant_id=tenant_id, owner_id=principal.user_id)
    page = await service.list(cursor=cursor, limit=limit)
    return SavedSearchListResponse(
        items=[_to_response(s) for s in page.items], next_cursor=page.next_cursor
    )


@router.post("", response_model=SavedSearchResponse, status_code=status.HTTP_201_CREATED)
async def create_saved_search(
    body: SavedSearchCreate,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> SavedSearchResponse:
    """Save a search (query + optional filters) under a name."""
    service = SavedSearchService(session, tenant_id=tenant_id, owner_id=principal.user_id)
    saved = await service.create(
        name=body.name,
        query=body.query,
        collection_id=body.collection_id,
        source=body.source,
        type=body.type,
    )
    await session.commit()
    return _to_response(saved)


@router.get("/{saved_search_id}", response_model=SavedSearchResponse)
async def get_saved_search(
    saved_search_id: UUID,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> SavedSearchResponse:
    """Get one of the caller's saved searches (404 if not visible)."""
    service = SavedSearchService(session, tenant_id=tenant_id, owner_id=principal.user_id)
    saved = await service.get(saved_search_id)
    if saved is None:
        raise NotFoundError("Saved search not found.")
    return _to_response(saved)


@router.patch("/{saved_search_id}", response_model=SavedSearchResponse)
async def update_saved_search(
    saved_search_id: UUID,
    body: SavedSearchUpdate,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> SavedSearchResponse:
    """Rename or change one of the caller's saved searches (404 if not visible)."""
    service = SavedSearchService(session, tenant_id=tenant_id, owner_id=principal.user_id)
    fields = body.model_fields_set
    saved = await service.update(
        saved_search_id,
        name=body.name,
        query=body.query,
        collection_id=body.collection_id,
        set_collection_id="collection_id" in fields,
        source=body.source,
        set_source="source" in fields,
        type=body.type,
        set_type="type" in fields,
    )
    if saved is None:
        raise NotFoundError("Saved search not found.")
    await session.commit()
    return _to_response(saved)


@router.delete("/{saved_search_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_search(
    saved_search_id: UUID,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> Response:
    """Delete one of the caller's saved searches (404 if not visible)."""
    service = SavedSearchService(session, tenant_id=tenant_id, owner_id=principal.user_id)
    deleted = await service.delete(saved_search_id)
    if not deleted:
        raise NotFoundError("Saved search not found.")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
