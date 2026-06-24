"""Sources routes — list / add / sync / delete (#20, ADR-0009 §5).

Contract-first: shapes match ``contracts/openapi.yaml`` (``SourceCreate`` →
``Source``; ``SourceList`` with cursor pagination; ``204`` delete; ``200``/``202``
sync). This module exposes a module-level ``router`` and is **auto-discovered**
(``api/v1/__init__.py`` scans for it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all
orchestration (ownership/tenancy enforcement, connector resolution + SSRF check,
audit, the sync enqueue, the cursor codec) lives in ``services.sources_service``;
this layer only (de)serialises and threads correlation context.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A source's
``tenant_id``/``owner_id`` come from the resolved principal (the token, never
request input). A source in another tenant or owned by another user is reported
as **404** (existence non-disclosure): the service returns ``None``/``False`` and
this router maps that to ``NotFoundError``, never 403. Every route requires the
bearer token; unauthenticated → 401 via ``current_user`` (INV-4).

**SSRF (ADR-0009 §3).** ``POST /sources`` validates + SSRF-checks the URL via the
connector; a blocked or invalid URL raises a typed ``ConnectorConfigError`` →
**422** with a Problem ``code`` (``url_blocked`` for an SSRF rejection). Mapped by
the global exception handler (the error is an ``AppError`` subclass), so this
router does not special-case it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    extract_request_id,
)
from app.core.errors import NotFoundError
from app.domain.entities import Source, SourceStatus, WebSourceMode
from app.services.sources_service import SourcePage, SourcesService

router = APIRouter(prefix="/sources", tags=["sources"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class SourceConfigResponse(BaseModel):
    """``#/components/schemas/SourceConfig`` — the web connector's ``{url, mode}``.

    ``additionalProperties: false`` in the contract: the internal ``collection_id``
    the service stores on the source config is **not** exposed — only ``url`` and
    ``mode`` are projected onto the wire. The frozen contract marks **both**
    ``url`` and ``mode`` required, so ``mode`` is non-null on every response: it is
    populated from a URL heuristic at creation and refined during sync.
    """

    model_config = {"extra": "forbid"}

    url: str
    mode: WebSourceMode


class SourceResponse(BaseModel):
    """``#/components/schemas/Source`` — the wire projection of a source."""

    model_config = {"extra": "forbid"}

    id: UUID
    type: str
    config: SourceConfigResponse
    status: SourceStatus
    indexed_count: int
    last_synced_at: datetime | None = None
    last_error: str | None = None
    owner_id: UUID
    created_at: datetime
    updated_at: datetime


class SourceListResponse(BaseModel):
    """``#/components/schemas/SourceList`` — a cursor-paginated page."""

    model_config = {"extra": "forbid"}

    items: list[SourceResponse]
    next_cursor: str | None = None


class SourceCreateRequest(BaseModel):
    """``#/components/schemas/SourceCreate`` — ``{type, url}`` for the web connector."""

    model_config = {"extra": "forbid"}

    type: str
    url: str


# --- Serialisation helpers --------------------------------------------------


def _config_response(config: dict[str, object]) -> SourceConfigResponse:
    """Project the stored config to the wire ``{url, mode}`` (drops internal keys).

    ``mode`` is contract-required (non-null): every stored web-source config gets
    a ``mode`` at creation (URL heuristic, refined during sync). As defence in
    depth — so a legacy/partial row can never emit a contract-invalid response —
    a missing or unrecognised ``mode`` falls back to ``page`` (the safe default).
    """
    url = config.get("url")
    mode_raw = config.get("mode")
    try:
        mode = WebSourceMode(mode_raw) if isinstance(mode_raw, str) else WebSourceMode.PAGE
    except ValueError:
        mode = WebSourceMode.PAGE
    return SourceConfigResponse(url=str(url) if url is not None else "", mode=mode)


def _to_response(source: Source) -> SourceResponse:
    return SourceResponse(
        id=source.id,
        type=source.type,
        config=_config_response(source.config),
        status=source.status,
        indexed_count=source.indexed_count,
        last_synced_at=source.last_synced_at,
        last_error=source.last_error,
        owner_id=source.owner_id,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _to_list_response(page: SourcePage) -> SourceListResponse:
    return SourceListResponse(
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
) -> SourcesService:
    """Assemble the per-request service from the identity + audit seams."""
    return SourcesService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=SourceListResponse, response_model_exclude_none=True)
async def list_sources(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SourceListResponse:
    """List the caller's own connected sources (cursor-paginated, newest first)."""
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
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_source(
    body: SourceCreateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> SourceResponse:
    """Add a source (first connector — a Web URL); enqueue its first sync.

    Validates + SSRF-checks the URL (ADR-0009 §3): a blocked or invalid URL is a
    **422** (``url_blocked`` for SSRF; ``unsupported_source_type`` for an unknown
    ``type``) raised by the service as a typed ``AppError`` and mapped by the
    global handler. On accept, a ``Source`` ``status=pending`` is returned and the
    sync runs async (never in the request path).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    source = await service.add(source_type=body.type, url=body.url)
    await session.commit()
    return _to_response(source)


@router.post("/{source_id}/sync", response_model=SourceResponse, response_model_exclude_none=True)
async def sync_source(
    source_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> SourceResponse:
    """Re-sync one of the caller's sources (re-fetch + re-index); else 404.

    Returns **202** with status ``syncing`` when a fresh sync was enqueued, or
    **200** (no-op) when the source was already ``syncing`` (the contract's two
    success codes). A non-owner or cross-tenant source is **404** (INV-1/INV-2).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    result = await service.resync(source_id)
    if result is None:
        raise NotFoundError("Source not found.")
    source, enqueued = result
    await session.commit()
    response.status_code = status.HTTP_202_ACCEPTED if enqueued else status.HTTP_200_OK
    return _to_response(source)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Delete one of the caller's sources; removes its docs + backing collection, else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    deleted = await service.delete(source_id)
    if not deleted:
        raise NotFoundError("Source not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
