"""Real SQL read-back preserves media evidence provenance and permissions."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    TranscriptRepository,
    TranscriptSegmentInput,
    TranscriptSpeakerInput,
    UserRepository,
)
from app.domain.entities import DocumentKind, DocumentStatus, Role
from app.domain.retrieval import RetrievedPassage
from app.retrieval import RetrievalService
from app.services.tools.handles import EvidenceHandles

import app.db.models  # noqa: F401  isort: skip


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as current:
            yield current
    finally:
        await engine.dispose()


async def _seed_media(
    session: AsyncSession,
    *,
    kind: DocumentKind,
    status: DocumentStatus = DocumentStatus.READY,
) -> tuple[Principal, Principal, RetrievedPassage]:
    tenant = await TenantRepository(session).create(name="Media read tenant")
    users = UserRepository(session, tenant.id)
    owner = await users.create(
        email=f"owner-{uuid.uuid4()}@example.test", password_hash="x", roles=[Role.MEMBER]
    )
    other = await users.create(
        email=f"other-{uuid.uuid4()}@example.test", password_hash="x", roles=[Role.MEMBER]
    )
    collection = await CollectionRepository(session, tenant.id).create(
        owner_id=owner.id, name="Media evidence"
    )
    document_id = uuid.uuid4()
    mime_type = "audio/mpeg" if kind is DocumentKind.AUDIO else "video/mp4"
    document = await DocumentRepository(session, tenant.id).create(
        document_id=document_id,
        owner_id=owner.id,
        collection_id=collection.id,
        filename="interview.mp3" if kind is DocumentKind.AUDIO else "interview.mp4",
        mime_type=mime_type,
        size_bytes=4096,
        storage_key=f"{tenant.id}/media/{document_id}",
        acl_enforced=False,
        status=status,
        kind=kind,
    )
    document = await DocumentRepository(session, tenant.id).update_media_metadata(
        document.id,
        kind=kind,
        duration_ms=6_000,
        transcript_language="en",
        transcription_model="test-transcriber",
    )
    assert document is not None

    text = "The meeting starts now."
    segment_id = uuid.uuid4()
    await TranscriptRepository(session, tenant.id).replace_for_document(
        document.id,
        speakers=[TranscriptSpeakerInput(speaker_id="speaker-1")],
        segments=[
            TranscriptSegmentInput(
                id=segment_id,
                ordinal=0,
                speaker_id="speaker-1",
                start_ms=1_250,
                end_ms=3_400,
                char_start=0,
                char_end=len(text),
                text=text,
            )
        ],
    )
    chunk = (
        await ChunkRepository(session, tenant.id).replace_for_document(
            document.id,
            [
                ChunkInput(
                    text=text,
                    char_start=0,
                    char_end=len(text),
                    time_start_ms=1_250,
                    time_end_ms=3_400,
                    transcript_segment_id=segment_id,
                    speaker_id="speaker-1",
                    speaker_name="Jordan",
                )
            ],
        )
    )[0]
    source_passage = RetrievedPassage(
        chunk_id=chunk.id,
        document_id=document.id,
        document_name=document.filename,
        ord=chunk.ord,
        text=chunk.text,
        char_start=chunk.char_start,
        char_end=chunk.char_end,
        score=1.0,
        time_start_ms=chunk.time_start_ms,
        time_end_ms=chunk.time_end_ms,
        transcript_segment_id=chunk.transcript_segment_id,
        speaker_id=chunk.speaker_id,
        speaker_name=chunk.speaker_name,
    )
    return (
        Principal(user_id=owner.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
        Principal(user_id=other.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
        source_passage,
    )


@pytest.mark.parametrize("kind", [DocumentKind.AUDIO, DocumentKind.VIDEO], ids=["audio", "video"])
async def test_read_passages_preserves_media_provenance_and_stable_handle(
    session: AsyncSession, kind: DocumentKind
) -> None:
    owner, _, source_passage = await _seed_media(session, kind=kind)
    retrieval = RetrievalService(session, gateway=cast(Any, None))

    handles = EvidenceHandles()
    source_handle = handles.passage(source_passage)
    passages = await retrieval.read_passages(principal=owner, chunk_ids=[source_passage.chunk_id])

    assert len(passages) == 1
    rehydrated = passages[0]
    assert (
        rehydrated.time_start_ms,
        rehydrated.time_end_ms,
        rehydrated.transcript_segment_id,
        rehydrated.speaker_id,
        rehydrated.speaker_name,
    ) == (
        1_250,
        3_400,
        source_passage.transcript_segment_id,
        "speaker-1",
        "Jordan",
    )
    assert handles.passage(rehydrated) == source_handle


async def test_read_passages_excludes_media_for_wrong_owner(session: AsyncSession) -> None:
    _, other, source_passage = await _seed_media(session, kind=DocumentKind.AUDIO)
    retrieval = RetrievalService(session, gateway=cast(Any, None))

    assert await retrieval.read_passages(principal=other, chunk_ids=[source_passage.chunk_id]) == []


async def test_read_passages_excludes_nonready_media(session: AsyncSession) -> None:
    owner, _, source_passage = await _seed_media(
        session, kind=DocumentKind.VIDEO, status=DocumentStatus.PENDING
    )
    retrieval = RetrievalService(session, gateway=cast(Any, None))

    assert await retrieval.read_passages(principal=owner, chunk_ids=[source_passage.chunk_id]) == []
