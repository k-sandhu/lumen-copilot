"""JSON-only direct multipart upload control-plane routes (spec 0008)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    ObjectStoreDep,
    SettingsDep,
    extract_request_id,
)
from app.api.v1.documents import DocumentResponse, _to_response
from app.core.errors import AppError, ConflictError, NotFoundError
from app.domain.entities import DocumentUploadState
from app.services.document_service import DocumentView
from app.services.document_upload_service import (
    CompletePartInput,
    DocumentUploadService,
    SignedPartView,
    UploadCompletionRejected,
    UploadControlOperation,
    UploadSessionView,
)

router = APIRouter(prefix="/document-uploads", tags=["document-uploads"])


class DocumentUploadCreate(BaseModel):
    model_config = {"extra": "forbid"}

    # Cross-field/suffix/size policy is deliberately evaluated in the service so
    # authenticated semantic rejections can emit durable INV-6 evidence. Schema
    # constraints remain contract-visible through json_schema_extra.
    filename: str = Field(json_schema_extra={"minLength": 1, "maxLength": 512})
    mime_type: str = Field(json_schema_extra={"minLength": 1, "maxLength": 255})
    size_bytes: int = Field(json_schema_extra={"minimum": 1})
    collection_id: UUID
    last_modified_at: datetime | None = None


class UploadedPartResponse(BaseModel):
    model_config = {"extra": "forbid"}
    part_number: int
    etag: str
    size_bytes: int


class UploadSessionResponse(BaseModel):
    model_config = {"extra": "forbid"}

    id: UUID
    document_id: UUID
    state: Literal["initiated", "completing", "completed", "aborted", "expired", "failed"]
    filename: str
    mime_type: str
    size_bytes: int
    collection_id: UUID
    part_size_bytes: int
    part_count: int
    completed_parts: list[UploadedPartResponse]
    expires_at: datetime
    error: str | None = None
    document: DocumentResponse | None
    created_at: datetime
    updated_at: datetime


class PartNumberList(BaseModel):
    model_config = {"extra": "forbid"}
    part_numbers: list[int] = Field(
        json_schema_extra={"minItems": 1, "maxItems": 100, "uniqueItems": True}
    )


class SignedPartResponse(BaseModel):
    model_config = {"extra": "forbid"}
    part_number: int
    url: str
    expires_at: datetime
    required_headers: dict[str, str]


class SignedPartListResponse(BaseModel):
    model_config = {"extra": "forbid"}
    items: list[SignedPartResponse]


class CompletePart(BaseModel):
    model_config = {"extra": "forbid"}
    part_number: int = Field(json_schema_extra={"minimum": 1, "maximum": 10_000})
    etag: str = Field(
        json_schema_extra={
            "minLength": 1,
            "maxLength": 512,
            "pattern": r"^[^\u0000-\u001F\u007F]{1,512}$",
        }
    )


class CompleteUpload(BaseModel):
    model_config = {"extra": "forbid"}
    parts: list[CompletePart] = Field(json_schema_extra={"minItems": 1, "maxItems": 10_000})


def _service(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> DocumentUploadService:
    return DocumentUploadService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        store=store,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
        allowed_content_types=settings.upload_allowed_content_types,
        max_document_bytes=settings.max_upload_bytes,
        max_media_bytes=settings.max_media_upload_bytes,
        part_size_bytes=settings.upload_part_size_bytes,
        max_parts=settings.upload_max_parts,
        sign_batch_size=settings.upload_sign_batch_size,
        session_ttl_seconds=settings.upload_session_ttl_seconds,
        presign_ttl_seconds=settings.s3_presign_ttl_seconds,
    )


def _session_response(view: UploadSessionView) -> UploadSessionResponse:
    upload = view.upload
    document = (
        _to_response(DocumentView(document=view.document, chunk_count=0))
        if view.document is not None
        else None
    )
    return UploadSessionResponse(
        id=upload.id,
        document_id=upload.document_id,
        state=upload.state.value,
        filename=upload.filename,
        mime_type=upload.mime_type,
        size_bytes=upload.size_bytes,
        collection_id=upload.collection_id,
        part_size_bytes=upload.part_size_bytes,
        part_count=upload.part_count,
        completed_parts=[
            UploadedPartResponse(
                part_number=part.part_number, etag=part.etag, size_bytes=part.size_bytes
            )
            for part in view.completed_parts
        ],
        expires_at=upload.expires_at,
        error=upload.error,
        document=document,
        created_at=upload.created_at,
        updated_at=upload.updated_at,
    )


def _signed_part(part: SignedPartView) -> SignedPartResponse:
    return SignedPartResponse(
        part_number=part.part_number,
        url=part.url,
        expires_at=part.expires_at,
        required_headers=part.required_headers,
    )


async def _commit_rejection(
    session: DbSession,
    service: DocumentUploadService,
    *,
    operation: UploadControlOperation,
    resource_type: str,
    resource_id: UUID,
    error: AppError,
    permission_denied: bool = False,
    preserve_transaction: bool = False,
) -> None:
    """Commit durable rejection evidence without committing partial work.

    Ordinary failures first discard the request transaction, then commit only
    the audit event. Terminal provider-verification failures and crash recovery
    transitions explicitly opt into preserving the current transaction so the
    state change and its error evidence remain atomic.
    """
    if not preserve_transaction:
        await session.rollback()
    await service.audit_rejection(
        operation=operation,
        resource_type=resource_type,
        resource_id=resource_id,
        error=error,
        permission_denied=permission_denied,
    )
    await session.commit()


@router.post("", response_model=UploadSessionResponse, status_code=status.HTTP_201_CREATED)
async def initiate_upload(
    body: DocumentUploadCreate,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> UploadSessionResponse:
    service = _service(request, session, principal, tenant_id, make_audit_sink, store, settings)
    try:
        view = await service.initiate(
            collection_id=body.collection_id,
            filename=body.filename,
            mime_type=body.mime_type,
            size_bytes=body.size_bytes,
            last_modified_at=body.last_modified_at,
        )
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="initiate",
            resource_type="collection",
            resource_id=body.collection_id,
            error=exc,
        )
        raise
    if view is None:
        error = NotFoundError("Collection not found.")
        await _commit_rejection(
            session,
            service,
            operation="initiate",
            resource_type="collection",
            resource_id=body.collection_id,
            error=error,
            permission_denied=True,
        )
        raise error
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await service.cleanup_failed_initiation(view.upload)
        raise
    return _session_response(view)


@router.get("/{upload_id}", response_model=UploadSessionResponse)
async def get_upload(
    upload_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> UploadSessionResponse:
    service = _service(request, session, principal, tenant_id, make_audit_sink, store, settings)
    try:
        view = await service.get(upload_id)
    except UploadCompletionRejected as exc:
        await _commit_rejection(
            session,
            service,
            operation="get",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
            preserve_transaction=True,
        )
        raise AppError(exc.detail, status=exc.status, code=exc.code, title=exc.title) from exc
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="get",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        raise
    if view is None:
        error = NotFoundError("Upload not found.")
        await _commit_rejection(
            session,
            service,
            operation="get",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
            permission_denied=True,
        )
        raise error
    await session.commit()
    return _session_response(view)


@router.post("/{upload_id}/parts", response_model=SignedPartListResponse)
async def sign_parts(
    upload_id: UUID,
    body: PartNumberList,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> SignedPartListResponse:
    service = _service(request, session, principal, tenant_id, make_audit_sink, store, settings)
    try:
        current = await service.expire_if_needed(upload_id)
    except UploadCompletionRejected as exc:
        await _commit_rejection(
            session,
            service,
            operation="sign_parts",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
            preserve_transaction=True,
        )
        raise AppError(exc.detail, status=exc.status, code=exc.code, title=exc.title) from exc
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="sign_parts",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        raise
    if current is None:
        error: AppError = NotFoundError("Upload not found.")
        await _commit_rejection(
            session,
            service,
            operation="sign_parts",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
            permission_denied=True,
        )
        raise error
    if current.upload.state is DocumentUploadState.EXPIRED:
        error = ConflictError("Upload session has expired.", code="upload_session_expired")
        await service.audit_rejection(
            operation="sign_parts",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
        )
        await session.commit()
        raise error
    if current.upload.state is DocumentUploadState.COMPLETED:
        # expire_if_needed may have crash-recovered a COMPLETING object. Commit
        # that document/audit/enqueue transaction before surfacing the intended
        # illegal-transition response for a parts request.
        error = ConflictError(
            "A completed upload does not accept parts.", code="upload_state_conflict"
        )
        await service.audit_rejection(
            operation="sign_parts",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
        )
        await session.commit()
        raise error
    try:
        parts = await service.sign_parts(upload_id, body.part_numbers)
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="sign_parts",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        raise
    if parts is None:
        error = NotFoundError("Upload not found.")
        await _commit_rejection(
            session,
            service,
            operation="sign_parts",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
            permission_denied=True,
        )
        raise error
    return SignedPartListResponse(items=[_signed_part(part) for part in parts])


@router.delete("/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def abort_upload(
    upload_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> Response:
    service = _service(request, session, principal, tenant_id, make_audit_sink, store, settings)
    try:
        current = await service.expire_if_needed(upload_id)
    except UploadCompletionRejected as exc:
        await _commit_rejection(
            session,
            service,
            operation="abort",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
            preserve_transaction=True,
        )
        raise AppError(exc.detail, status=exc.status, code=exc.code, title=exc.title) from exc
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="abort",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        raise
    if current is None:
        error: AppError = NotFoundError("Upload not found.")
        await _commit_rejection(
            session,
            service,
            operation="abort",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
            permission_denied=True,
        )
        raise error
    if current.upload.state is DocumentUploadState.EXPIRED:
        await session.commit()
        response.status_code = status.HTTP_204_NO_CONTENT
        return response
    if current.upload.state is DocumentUploadState.COMPLETED:
        # Recovery is a successful durable transition even though aborting the
        # resulting document is illegal; never roll it back with the 409.
        error = ConflictError("A completed upload cannot be aborted.", code="upload_state_conflict")
        await service.audit_rejection(
            operation="abort",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
        )
        await session.commit()
        raise error
    try:
        result = await service.abort(upload_id)
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="abort",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        raise
    if result is None:
        error = NotFoundError("Upload not found.")
        await _commit_rejection(
            session,
            service,
            operation="abort",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
            permission_denied=True,
        )
        raise error
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/{upload_id}/complete", response_model=DocumentResponse)
async def complete_upload(
    upload_id: UUID,
    body: CompleteUpload,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> DocumentResponse:
    service = _service(request, session, principal, tenant_id, make_audit_sink, store, settings)
    try:
        current = await service.expire_if_needed(upload_id)
    except UploadCompletionRejected as exc:
        await _commit_rejection(
            session,
            service,
            operation="complete",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
            preserve_transaction=True,
        )
        raise AppError(exc.detail, status=exc.status, code=exc.code, title=exc.title) from exc
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="complete",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        raise
    if current is None:
        error: AppError = NotFoundError("Upload not found.")
        await _commit_rejection(
            session,
            service,
            operation="complete",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
            permission_denied=True,
        )
        raise error
    if current.upload.state is DocumentUploadState.EXPIRED:
        error = ConflictError("Upload session has expired.", code="upload_session_expired")
        await service.audit_rejection(
            operation="complete",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
        )
        await session.commit()
        raise error
    try:
        document = await service.complete(
            upload_id,
            [CompletePartInput(part.part_number, part.etag) for part in body.parts],
        )
    except UploadCompletionRejected as exc:
        # Persist the terminal failed state/object cleanup, then surface typed error.
        await service.audit_rejection(
            operation="complete",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        await session.commit()
        raise AppError(exc.detail, status=exc.status, code=exc.code, title=exc.title) from exc
    except AppError as exc:
        await _commit_rejection(
            session,
            service,
            operation="complete",
            resource_type="document_upload",
            resource_id=upload_id,
            error=exc,
        )
        raise
    if document is None:
        error = NotFoundError("Upload not found.")
        await _commit_rejection(
            session,
            service,
            operation="complete",
            resource_type="document_upload",
            resource_id=upload_id,
            error=error,
            permission_denied=True,
        )
        raise error
    await session.commit()
    return _to_response(DocumentView(document=document, chunk_count=0))
