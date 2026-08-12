"""Document metadata/text routes plus retired v1 byte-transfer endpoints.

Contract-first shapes match ``contracts/openapi.yaml``. Routers validate in,
call one service, and shape out (ADR-0004); ownership, tenancy, audit, and cursor
orchestration stay in ``services.document_service``. Binary ``POST /documents``
and ``GET /documents/{id}/content`` authenticate and return 410 without parsing
or streaming bytes. Clients use the v2 direct multipart and signed-access APIs.

**Tenancy + ownership (spec 0004 §2.1/§2.2).** The document's ``tenant_id`` and
``owner_id`` come from the resolved principal (``current_user`` / ``current_tenant``
— the token, never request input). A document in another tenant or owned by
another user is reported as **404**
(existence non-disclosure, INV-1/INV-2): the service returns ``None``/``False``
and this router maps that to ``NotFoundError``, never 403. Every route requires
the bearer token (contract ``bearerAuth``); unauthenticated → 401 via
``current_user`` (INV-4).

The retired binary routes return authenticated 410 responses with stable error
codes; they intentionally declare no body/file parameter, so FastAPI never
materializes upload bytes.
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
    ObjectStoreDep,
    SettingsDep,
    extract_request_id,
)
from app.core.errors import GoneError, NotFoundError
from app.domain.entities import DocumentKind, DocumentStatus
from app.services.document_service import (
    DocumentPage,
    DocumentService,
    DocumentView,
)

router = APIRouter(prefix="/documents", tags=["documents"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class DocumentResponse(BaseModel):
    """``#/components/schemas/Document`` — the wire projection of a document."""

    model_config = {"extra": "forbid"}

    id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    collection_id: UUID
    owner_id: UUID
    kind: DocumentKind
    duration_ms: int | None = None
    status: DocumentStatus
    error: str | None = None
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    """``#/components/schemas/DocumentList`` — a cursor-paginated page."""

    model_config = {"extra": "forbid"}

    items: list[DocumentResponse]
    next_cursor: str | None = None


class DocumentTextResponse(BaseModel):
    """``#/components/schemas/DocumentText`` — extracted plain text (#244)."""

    model_config = {"extra": "forbid"}

    text: str
    chunk_count: int
    truncated: bool


# --- Serialisation helpers --------------------------------------------------


def _to_response(view: DocumentView) -> DocumentResponse:
    d = view.document
    return DocumentResponse(
        id=d.id,
        filename=d.filename,
        mime_type=d.mime_type,
        size_bytes=d.size_bytes,
        collection_id=d.collection_id,
        owner_id=d.owner_id,
        kind=d.kind,
        duration_ms=d.duration_ms,
        status=d.status,
        error=d.error,
        chunk_count=view.chunk_count,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


def _to_list_response(page: DocumentPage) -> DocumentListResponse:
    return DocumentListResponse(
        items=[_to_response(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    request: Request,
) -> DocumentService:
    """Assemble the per-request service from the identity + adapter + audit seams.

    The object store is the injected #22 adapter (the only object-store caller).
    The audit sink is bound to the caller's tenant; ``request_id``/``source_ip``
    thread the correlation context into audit envelopes without the service
    touching the request.
    """
    return DocumentService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        object_store=object_store,
        audit=make_audit_sink(tenant_id),
        # The audit envelope requires a non-empty request_id / source_ip (spec
        # 0004 §2.4); fall back to a sentinel when the client supplied neither.
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=DocumentListResponse, response_model_exclude_none=True)
async def list_documents(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    collection_id: Annotated[UUID | None, Query()] = None,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DocumentListResponse:
    """List the caller's own documents (cursor-paginated, newest first).

    Optional filters mirror the contract: ``collection_id`` restricts to one
    collection, ``status`` to one lifecycle state, ``q`` to a filename substring
    (lexical, not semantic).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    page = await service.list_page(
        cursor=cursor,
        limit=limit,
        collection_id=collection_id,
        status=status_filter,
        filename_query=q,
    )
    return _to_list_response(page)


@router.post("")
async def upload_document_legacy(_principal: CurrentUser) -> None:
    """Retired binary ingress: authenticate, then fail before reading a body."""
    raise GoneError(
        "Binary uploads through FastAPI are retired; use POST /api/v2/document-uploads.",
        code="document_upload_retired",
    )


@router.get("/{document_id}", response_model=DocumentResponse, response_model_exclude_none=True)
async def get_document(
    document_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
) -> DocumentResponse:
    """Get one of the caller's documents (incl. ingestion status); else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    view = await service.get(document_id)
    if view is None:
        raise NotFoundError("Document not found.")
    return _to_response(view)


@router.get("/{document_id}/content")
async def get_document_content_legacy(document_id: UUID, _principal: CurrentUser) -> None:
    """Retired backend byte/redirect path; v2 returns a JSON capability."""
    del document_id
    raise GoneError(
        "Backend document bytes are retired; use POST /api/v2/documents/{id}/access-url.",
        code="document_content_retired",
    )


@router.get("/{document_id}/text", response_model=DocumentTextResponse)
async def get_document_text(
    document_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
) -> DocumentTextResponse:
    """The extracted plain text of a ready document (contract 0.6.0, #244).

    The viewer's text surface for formats a browser cannot render natively
    (DOCX/PPTX/XLSX). Visibility identical to ``/content`` (INV-1/INV-2 → 404);
    a visible document that is not ``ready`` → 409 ``document_not_ready``
    (INV-8), raised by the service as a typed ``ConflictError``.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    result = await service.get_text(document_id, max_bytes=settings.document_text_max_bytes)
    if result is None:
        raise NotFoundError("Document not found.")
    await session.commit()
    return DocumentTextResponse(
        text=result.text,
        chunk_count=result.chunk_count,
        truncated=result.truncated,
    )


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
) -> Response:
    """Delete one of the caller's documents (row + chunks + object); else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    deleted = await service.delete(document_id)
    if not deleted:
        raise NotFoundError("Document not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
