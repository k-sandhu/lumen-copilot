"""Signed object capabilities and timestamped transcript reads (spec 0008)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
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
from app.core.errors import NotFoundError
from app.services.document_upload_service import DocumentAccessService

router = APIRouter(prefix="/documents", tags=["documents"])


class AccessRequest(BaseModel):
    model_config = {"extra": "forbid"}
    purpose: Literal["preview", "download"]


class AccessResponse(BaseModel):
    model_config = {"extra": "forbid"}
    url: str
    filename: str
    mime_type: str
    size_bytes: int
    expires_at: datetime
    purpose: Literal["preview", "download"]
    supports_byte_ranges: bool


class TranscriptSpeakerResponse(BaseModel):
    model_config = {"extra": "forbid"}
    speaker_id: str
    display_name: str | None
    name_status: Literal["unknown", "inferred"]
    name_confidence: float | None
    name_method: Literal["self_introduction", "contextual_dialogue"] | None
    evidence_segment_ids: list[UUID]


class TranscriptSegmentResponse(BaseModel):
    model_config = {"extra": "forbid"}
    id: UUID
    ordinal: int
    speaker_id: str
    start_ms: int
    end_ms: int
    char_start: int
    char_end: int
    text: str
    confidence: float | None


class TranscriptResponse(BaseModel):
    model_config = {"extra": "forbid"}
    document_id: UUID
    duration_ms: int
    language: str | None
    transcription_model: str
    speakers: list[TranscriptSpeakerResponse]
    items: list[TranscriptSegmentResponse]
    next_cursor: str | None


def _service(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> DocumentAccessService:
    return DocumentAccessService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        store=store,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
        presign_ttl_seconds=settings.s3_presign_ttl_seconds,
    )


@router.post("/{document_id}/access-url", response_model=AccessResponse)
async def create_access_url(
    document_id: UUID,
    body: AccessRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
) -> AccessResponse:
    service = _service(request, session, principal, tenant_id, make_audit_sink, store, settings)
    capability = await service.create_access_url(document_id, purpose=body.purpose)
    if capability is None:
        await session.commit()
        raise NotFoundError("Document not found.")
    await session.commit()
    return AccessResponse(
        url=capability.url,
        filename=capability.document.filename,
        mime_type=capability.document.mime_type,
        size_bytes=capability.document.size_bytes,
        expires_at=capability.expires_at,
        purpose=body.purpose,
        supports_byte_ranges=capability.supports_byte_ranges,
    )


@router.get("/{document_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(
    document_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    store: ObjectStoreDep,
    settings: SettingsDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    around_ms: Annotated[int | None, Query(ge=0)] = None,
) -> TranscriptResponse:
    service = _service(request, session, principal, tenant_id, make_audit_sink, store, settings)
    page = await service.get_transcript(
        document_id, cursor=cursor, limit=limit, around_ms=around_ms
    )
    if page is None:
        await session.commit()
        raise NotFoundError("Document not found.")
    await session.commit()
    document = page.document
    assert document.duration_ms is not None
    assert document.transcription_model is not None
    return TranscriptResponse(
        document_id=document.id,
        duration_ms=document.duration_ms,
        language=document.transcript_language,
        transcription_model=document.transcription_model,
        speakers=[
            TranscriptSpeakerResponse(
                speaker_id=speaker.speaker_id,
                display_name=speaker.display_name,
                name_status=speaker.name_status,
                name_confidence=speaker.name_confidence,
                name_method=speaker.name_method,
                evidence_segment_ids=list(speaker.evidence_segment_ids),
            )
            for speaker in page.speakers
        ],
        items=[
            TranscriptSegmentResponse(
                id=segment.id,
                ordinal=segment.ordinal,
                speaker_id=segment.speaker_id,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                char_start=segment.char_start,
                char_end=segment.char_end,
                text=segment.text,
                confidence=segment.confidence,
            )
            for segment in page.items
        ],
        next_cursor=page.next_cursor,
    )
