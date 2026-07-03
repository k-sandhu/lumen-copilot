"""Documents routes — upload / list / get / content / delete (#28).

Contract-first: shapes match ``contracts/openapi.yaml`` (multipart upload →
``Document``; ``DocumentList`` with cursor pagination; ``DocumentStatus`` filter;
``302`` + ``Location`` presigned content). Routers validate in → call **one**
service → shape out (ADR-0004): all orchestration (ownership/tenancy enforcement,
object-store access, audit, the upload validation re-mapping, the cursor codec)
lives in ``services.document_service``; this layer only (de)serialises and
threads correlation context. Object storage is reached **only** through the #22
``storage/`` adapter (held by the service), never here; SQL only via ``db/``.

**Tenancy + ownership (spec 0004 §2.1/§2.2).** The document's ``tenant_id`` and
``owner_id`` come from the resolved principal (``current_user`` / ``current_tenant``
— the token, never request input). A document — or, on upload, the target
collection — in another tenant or owned by another user is reported as **404**
(existence non-disclosure, INV-1/INV-2): the service returns ``None``/``False``
and this router maps that to ``NotFoundError``, never 403. Every route requires
the bearer token (contract ``bearerAuth``); unauthenticated → 401 via
``current_user`` (INV-4).

**Upload negatives (the contract's distinct statuses).** Over-size → 413,
unsupported content-type → 415 (the service re-maps the #22 storage validation);
a missing/blank multipart field → 422 (FastAPI form validation + the guards
here). The ingestion task (#21) is OUT of scope — a successful upload leaves the
document ``status=pending`` as the seam the worker picks up.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Query, Request, Response, UploadFile, status
from fastapi.responses import RedirectResponse, StreamingResponse
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
from app.core.errors import NotFoundError, ValidationError
from app.domain.entities import DocumentStatus
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
    settings: SettingsDep,
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
        upload_allowed_content_types=settings.upload_allowed_content_types,
        max_upload_bytes=settings.max_upload_bytes,
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
    settings: SettingsDep,
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
        settings=settings,
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


@router.post(
    "",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def upload_document(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
    collection_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    """Upload a file into one of the caller's collections (multi-format).

    Stores the bytes (MinIO, #22) and creates a ``Document`` ``status=pending``;
    ingestion (#21) is enqueued by the service seam (not implemented here). The
    target collection must be the caller's — else 404 (INV-1/INV-2). Over-size →
    413, unsupported content-type → 415, missing/blank field → 422.
    """
    data = await file.read()
    # The declared content-type drives the allowlist check; default to the
    # generic octet-stream so a client that omits it is rejected by the allowlist
    # (415) rather than silently accepted.
    content_type = file.content_type or "application/octet-stream"
    filename = (file.filename or "").strip()
    if not filename:
        # The multipart part carried no usable filename — malformed (INV-8 → 422).
        raise ValidationError("upload is missing a filename", code="missing_filename")

    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )
    view = await service.upload(
        collection_id=collection_id,
        filename=filename,
        content_type=content_type,
        data=data,
    )
    if view is None:
        raise NotFoundError("Collection not found.")
    await session.commit()
    return _to_response(view)


@router.get("/{document_id}", response_model=DocumentResponse, response_model_exclude_none=True)
async def get_document(
    document_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
) -> DocumentResponse:
    """Get one of the caller's documents (incl. ingestion status); else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )
    view = await service.get(document_id)
    if view is None:
        raise NotFoundError("Document not found.")
    return _to_response(view)


@router.get("/{document_id}/content")
async def get_document_content(
    document_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    settings: SettingsDep,
) -> Response:
    """Download / view the original bytes; not visible → 404.

    Default (``DOCUMENT_CONTENT_REDIRECT=true``): respond **302** to a short-TTL
    presigned GET URL (CC-12) via the #22 ``ObjectStore.presign_get``, with the
    ``Location`` header — the client follows it and the bytes transfer directly
    from storage, not through the API process. When the flag is false, stream the
    bytes inline as ``application/octet-stream``. Audited ``document.downloaded``
    (INV-6) by the service.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )

    if settings.document_content_redirect:
        url = await service.presign_content(document_id)
        if url is None:
            raise NotFoundError("Document not found.")
        await session.commit()
        # 302 + Location per the frozen contract; the client must follow it.
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)

    content = await service.get_content(document_id)
    if content is None:
        raise NotFoundError("Document not found.")
    await session.commit()
    # Stream with the document's own (allowlisted) content-type — NOT a generic
    # octet-stream — so the browser can render it inline (a PDF/image/text preview
    # in the viewer's sandboxed iframe). Safe: the upload allowlist admits only
    # PDF / OOXML office / text-plain / markdown (never HTML/SVG), so an inline
    # response cannot carry active content, and the viewer iframe is sandboxed
    # regardless. This same-origin inline stream is what makes previews work with
    # no cross-origin CORS dependency when DOCUMENT_CONTENT_REDIRECT is off (#242).
    return StreamingResponse(
        iter((content.data,)),
        media_type=content.mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{content.filename}"',
            "Content-Length": str(len(content.data)),
        },
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
        settings=settings,
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
    settings: SettingsDep,
) -> Response:
    """Delete one of the caller's documents (row + chunks + object); else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        settings=settings,
        request=request,
    )
    deleted = await service.delete(document_id)
    if not deleted:
        raise NotFoundError("Document not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
