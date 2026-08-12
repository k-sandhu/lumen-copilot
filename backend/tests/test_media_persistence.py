"""Persistence seams for direct uploads and timestamped transcripts (#571)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
    DocumentUploadRepository,
    MessageRepository,
    TranscriptRepository,
    TranscriptSegmentInput,
    TranscriptSpeakerInput,
    UserRepository,
)
from app.domain.entities import (
    DocumentKind,
    DocumentStatus,
    DocumentUploadState,
    MessageRole,
    Role,
)

import app.db.models  # noqa: F401  isort: skip


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def two_tenants(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    from app.db.repositories import TenantRepository

    first = await TenantRepository(session).create(name="Acme")
    second = await TenantRepository(session).create(name="Globex")
    return first.id, second.id


async def _owner_collection(
    session: AsyncSession, tenant_id: uuid.UUID, *, email: str
) -> tuple[uuid.UUID, uuid.UUID]:
    user = await UserRepository(session, tenant_id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    collection = await CollectionRepository(session, tenant_id).create(
        owner_id=user.id, name="Media"
    )
    return user.id, collection.id


async def test_upload_sessions_are_owner_and_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    owner_id, collection_id = await _owner_collection(session, tenant_a, email="media-owner@a.test")
    upload_id = uuid.uuid4()
    document_id = uuid.uuid4()
    created = await DocumentUploadRepository(session, tenant_a).create(
        upload_id=upload_id,
        document_id=document_id,
        owner_id=owner_id,
        collection_id=collection_id,
        filename="meeting.mp4",
        mime_type="video/mp4",
        size_bytes=16_000_000,
        storage_key=f"{tenant_a}/quarantine/{document_id}/meeting.mp4",
        provider_upload_id="private-provider-id",
        part_size_bytes=8 * 1024 * 1024,
        part_count=2,
        expires_at=created_at_plus_hour(),
    )

    assert created.state is DocumentUploadState.INITIATED
    reloaded = await DocumentUploadRepository(session, tenant_a).get_for_owner(upload_id, owner_id)
    assert reloaded is not None
    assert reloaded.id == created.id
    assert reloaded.document_id == created.document_id
    assert reloaded.provider_upload_id == created.provider_upload_id
    assert (
        await DocumentUploadRepository(session, tenant_b).get_for_owner(upload_id, owner_id) is None
    )


async def test_transcript_replace_and_media_chunk_timestamps_round_trip(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    owner_id, collection_id = await _owner_collection(
        session, tenant_a, email="transcript-owner@a.test"
    )
    document_id = uuid.uuid4()
    document = await DocumentRepository(session, tenant_a).create(
        document_id=document_id,
        owner_id=owner_id,
        collection_id=collection_id,
        filename="meeting.mp3",
        mime_type="audio/mpeg",
        size_bytes=1234,
        storage_key=f"{tenant_a}/quarantine/{document_id}/meeting.mp3",
        acl_enforced=False,
        kind=DocumentKind.AUDIO,
    )
    document = await DocumentRepository(session, tenant_a).update_media_metadata(
        document.id,
        kind=DocumentKind.AUDIO,
        duration_ms=6_000,
        transcript_language="en",
        transcription_model="x-ai/grok-stt-1.0",
    )
    assert document is not None
    assert document.duration_ms == 6_000

    first_segment_id = uuid.uuid4()
    speakers, segments = await TranscriptRepository(session, tenant_a).replace_for_document(
        document.id,
        speakers=[
            TranscriptSpeakerInput(
                speaker_id="speaker-1",
                display_name="John",
                name_status="inferred",
                name_confidence=0.99,
                name_method="self_introduction",
                evidence_segment_ids=(first_segment_id,),
            )
        ],
        segments=[
            TranscriptSegmentInput(
                id=first_segment_id,
                ordinal=0,
                speaker_id="speaker-1",
                start_ms=1_000,
                end_ms=3_000,
                char_start=0,
                char_end=22,
                text="Hello, my name is John",
                confidence=0.98,
            )
        ],
    )
    assert speakers[0].display_name == "John"
    assert segments[0].start_ms == 1_000
    assert await TranscriptRepository(session, tenant_b).list_segments(document.id) == []

    chunk = (
        await ChunkRepository(session, tenant_a).replace_for_document(
            document.id,
            [
                ChunkInput(
                    text="Hello, my name is John",
                    char_start=0,
                    char_end=22,
                    time_start_ms=1_000,
                    time_end_ms=3_000,
                    transcript_segment_id=first_segment_id,
                    speaker_id="speaker-1",
                    speaker_name="John",
                )
            ],
        )
    )[0]
    assert (chunk.time_start_ms, chunk.time_end_ms) == (1_000, 3_000)
    assert chunk.transcript_segment_id == first_segment_id


def created_at_plus_hour() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def test_transcription_config_rejects_overlap_not_smaller_than_chunk() -> None:
    with pytest.raises(ValueError, match="OVERLAP|overlap"):
        Settings(
            TRANSCRIPTION_CHUNK_SECONDS="10",
            TRANSCRIPTION_CHUNK_OVERLAP_SECONDS="10",
        )


def test_upload_sign_batch_cannot_diverge_from_frozen_client_contract() -> None:
    with pytest.raises(ValueError, match="UPLOAD_SIGN_BATCH_SIZE"):
        Settings(UPLOAD_SIGN_BATCH_SIZE="99")


def test_incomplete_multipart_lifecycle_requires_at_least_one_day() -> None:
    with pytest.raises(ValueError, match="UPLOAD_INCOMPLETE_LIFECYCLE_DAYS"):
        Settings(UPLOAD_INCOMPLETE_LIFECYCLE_DAYS="0")


async def test_transcript_repository_rejects_duplicate_ids_and_out_of_duration(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_id, _ = two_tenants
    owner_id, collection_id = await _owner_collection(
        session, tenant_id, email="invalid-transcript@a.test"
    )
    document = await DocumentRepository(session, tenant_id).create(
        owner_id=owner_id,
        collection_id=collection_id,
        filename="short.mp3",
        mime_type="audio/mpeg",
        size_bytes=1,
        storage_key=f"{tenant_id}/short.mp3",
        acl_enforced=False,
        kind=DocumentKind.AUDIO,
    )
    document = await DocumentRepository(session, tenant_id).update_media_metadata(
        document.id,
        kind=DocumentKind.AUDIO,
        duration_ms=1_000,
        transcript_language="en",
        transcription_model="x-ai/grok-stt-1.0",
    )
    assert document is not None
    segment_id = uuid.uuid4()
    speakers = [TranscriptSpeakerInput(speaker_id="speaker-1")]
    duplicate = TranscriptSegmentInput(
        id=segment_id,
        ordinal=0,
        speaker_id="speaker-1",
        start_ms=0,
        end_ms=500,
        char_start=0,
        char_end=2,
        text="Hi",
    )
    with pytest.raises(ValueError, match="ids must be unique"):
        await TranscriptRepository(session, tenant_id).replace_for_document(
            document.id,
            speakers=speakers,
            segments=[
                duplicate,
                TranscriptSegmentInput(
                    id=segment_id,
                    ordinal=1,
                    speaker_id="speaker-1",
                    start_ms=500,
                    end_ms=900,
                    char_start=3,
                    char_end=6,
                    text="Bye",
                ),
            ],
        )
    with pytest.raises(ValueError, match="time span"):
        await TranscriptRepository(session, tenant_id).replace_for_document(
            document.id,
            speakers=speakers,
            segments=[
                TranscriptSegmentInput(
                    id=uuid.uuid4(),
                    ordinal=0,
                    speaker_id="speaker-1",
                    start_ms=0,
                    end_ms=1_001,
                    char_start=0,
                    char_end=2,
                    text="Hi",
                )
            ],
        )


async def test_citation_media_provenance_fails_closed(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_id, _ = two_tenants
    owner_id, collection_id = await _owner_collection(
        session, tenant_id, email="citation-provenance@a.test"
    )
    documents = DocumentRepository(session, tenant_id)
    ordinary = await documents.create(
        owner_id=owner_id,
        collection_id=collection_id,
        filename="ordinary.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_key=f"{tenant_id}/ordinary.txt",
        acl_enforced=False,
    )
    ordinary_chunk = await ChunkRepository(session, tenant_id).add(
        document_id=ordinary.id,
        ord=0,
        text="ordinary",
        char_start=0,
        char_end=8,
    )
    media = await documents.create(
        owner_id=owner_id,
        collection_id=collection_id,
        filename="meeting.mp3",
        mime_type="audio/mpeg",
        size_bytes=1,
        storage_key=f"{tenant_id}/meeting.mp3",
        acl_enforced=False,
        kind=DocumentKind.AUDIO,
        status=DocumentStatus.READY,
    )
    media = await documents.update_media_metadata(
        media.id,
        kind=DocumentKind.AUDIO,
        duration_ms=5_000,
        transcript_language="en",
        transcription_model="x-ai/grok-stt-1.0",
    )
    assert media is not None
    segment_id = uuid.uuid4()
    other_segment_id = uuid.uuid4()
    await TranscriptRepository(session, tenant_id).replace_for_document(
        media.id,
        speakers=[TranscriptSpeakerInput(speaker_id="speaker-1")],
        segments=[
            TranscriptSegmentInput(
                id=segment_id,
                ordinal=0,
                speaker_id="speaker-1",
                start_ms=1_000,
                end_ms=3_000,
                char_start=0,
                char_end=5,
                text="hello",
            ),
            TranscriptSegmentInput(
                id=other_segment_id,
                ordinal=1,
                speaker_id="speaker-1",
                start_ms=3_000,
                end_ms=4_000,
                char_start=6,
                char_end=11,
                text="again",
            ),
        ],
    )
    media_chunk = await ChunkRepository(session, tenant_id).add(
        document_id=media.id,
        ord=0,
        text="hello",
        char_start=0,
        char_end=5,
        time_start_ms=1_000,
        time_end_ms=3_000,
        transcript_segment_id=segment_id,
        speaker_id="speaker-1",
        speaker_name=None,
    )
    chat = await ChatSessionRepository(session, tenant_id).create(
        owner_id=owner_id, model="test/model"
    )
    message = await MessageRepository(session, tenant_id).add(
        session_id=chat.id, role=MessageRole.ASSISTANT, content="answer"
    )
    citations = CitationRepository(session, tenant_id)
    with pytest.raises(ValueError, match="ordinary document"):
        await citations.add(
            message_id=message.id,
            chunk_id=ordinary_chunk.id,
            char_start=0,
            char_end=1,
            speaker_id="speaker-1",
        )
    with pytest.raises(ValueError, match="outside its source chunk"):
        await citations.add(
            message_id=message.id,
            chunk_id=media_chunk.id,
            char_start=0,
            char_end=1,
            time_start_ms=0,
            time_end_ms=4_000,
        )
    with pytest.raises(ValueError, match="source chunk"):
        await citations.add(
            message_id=message.id,
            chunk_id=media_chunk.id,
            char_start=0,
            char_end=1,
            time_start_ms=1_000,
            time_end_ms=3_000,
            transcript_segment_id=other_segment_id,
        )
    with pytest.raises(ValueError, match="speaker"):
        await citations.add(
            message_id=message.id,
            chunk_id=media_chunk.id,
            char_start=0,
            char_end=1,
            time_start_ms=1_000,
            time_end_ms=3_000,
            transcript_segment_id=segment_id,
            speaker_id="arbitrary-speaker",
        )
