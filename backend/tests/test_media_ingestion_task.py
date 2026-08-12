"""Checkpointed media-ingestion persistence at the Celery orchestration seam."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401  isort: skip
import app.db.session as db_session
import app.tasks.ingest as ingest_module
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    TranscriptionCheckpointRepository,
    TranscriptRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import DocumentKind, DocumentStatus, Role
from app.domain.llm import Embedding, Transcription, TranscriptionWord
from app.ingestion.media import (
    MediaSpan,
    StitchedWord,
    build_transcript_chunks,
    build_transcript_segments,
    infer_speaker_names,
)


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    old_engine = db_session._engine
    old_maker = db_session._sessionmaker
    db_session._engine = engine
    db_session._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield
    finally:
        db_session._engine = old_engine
        db_session._sessionmaker = old_maker
        await engine.dispose()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "k",
        "S3_SECRET_KEY": "s",
        "S3_BUCKET": "b",
        "OPENROUTER_API_KEY": "offline-test",
        "LLM_EMBEDDING_DIMENSIONS": "8",
        "INGESTION_CHUNK_SIZE": "100",
        "INGESTION_CHUNK_OVERLAP": "10",
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


async def _seed_media() -> tuple[uuid.UUID, uuid.UUID]:
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        owner = await UserRepository(session, tenant.id).create(
            email="media@acme.test", password_hash="hash", roles=[Role.MEMBER]
        )
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=owner.id, name="Meetings"
        )
        document = await DocumentRepository(session, tenant.id).create(
            owner_id=owner.id,
            collection_id=collection.id,
            filename="meeting.wav",
            mime_type="audio/wav",
            size_bytes=100,
            storage_key=f"{tenant.id}/quarantine/meeting.wav",
            acl_enforced=False,
            status=DocumentStatus.PENDING,
            kind=DocumentKind.AUDIO,
        )
    return tenant.id, document.id


class _Gateway:
    def __init__(self, *, model: str) -> None:
        self.model = model
        self.transcribe_calls: list[Path] = []

    async def transcribe(self, audio_path: Path) -> Transcription:
        self.transcribe_calls.append(audio_path)
        return Transcription(
            text="checkpointed speech",
            words=(
                TranscriptionWord(
                    text="checkpointed",
                    start_ms=100,
                    end_ms=500,
                    speaker_label="provider-speaker",
                ),
                TranscriptionWord(
                    text="speech",
                    start_ms=550,
                    end_ms=900,
                    speaker_label="provider-speaker",
                ),
            ),
            language="en",
            model=self.model,
        )

    async def embed(self, inputs: Sequence[str]) -> list[Embedding]:
        return [Embedding(vector=[0.25] * 8, model="fake") for _ in inputs]


async def test_transcription_retry_reuses_paid_chunk_checkpoints(
    sqlite_engine: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        TRANSCRIPTION_CHUNK_SECONDS="10",
        TRANSCRIPTION_CHUNK_OVERLAP_SECONDS="1",
    )
    tenant_id, document_id = await _seed_media()
    normalized = tmp_path / "normalized.wav"
    normalized.write_bytes(b"bounded normalized audio")
    extracted: list[int] = []

    async with db_session.session_scope() as session:
        await TranscriptionCheckpointRepository(session, tenant_id).upsert(
            document_id,
            chunk_index=0,
            model="retired/stt-route",
            start_ms=0,
            end_ms=10_000,
            language="en",
            words=[
                {
                    "text": "obsolete",
                    "start_ms": 0,
                    "end_ms": 100,
                    "speaker_label": "old-speaker",
                    "confidence": None,
                }
            ],
        )

    async def _fake_extract(
        _source: Path,
        destination: Path,
        *,
        span: MediaSpan,
        ffmpeg_path: str,
    ) -> None:
        del ffmpeg_path
        index = span.index
        extracted.append(index)
        destination.write_bytes(f"chunk-{index}".encode())

    monkeypatch.setattr(ingest_module, "extract_audio_chunk", _fake_extract)

    first_gateway = _Gateway(model=settings.transcription_model)
    first_run_id = uuid.uuid4()
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert (
            await DocumentRepository(session, tenant_id).claim_ingestion(
                document_id,
                ingestion_run_id=first_run_id,
                stale_before=datetime.now(UTC) - timedelta(minutes=30),
            )
            is not None
        )
    first = await ingest_module._transcribe_chunks(
        tenant_id,
        document_id,
        ingestion_run_id=first_run_id,
        normalized_audio=normalized,
        duration_ms=15_000,
        settings=settings,
        gateway=first_gateway,  # type: ignore[arg-type]
        workdir=tmp_path,
    )
    assert len(first) == 2
    assert len(first_gateway.transcribe_calls) == 2
    assert extracted == [0, 1]

    retry_run_id = uuid.uuid4()
    async with db_session.tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        assert (
            await documents.finish_ingestion(
                document_id,
                ingestion_run_id=first_run_id,
                status=DocumentStatus.READY,
            )
            is not None
        )
        assert (
            await documents.claim_ingestion(
                document_id,
                ingestion_run_id=retry_run_id,
                stale_before=datetime.now(UTC) - timedelta(minutes=30),
            )
            is not None
        )
    retry_gateway = _Gateway(model=settings.transcription_model)
    retry = await ingest_module._transcribe_chunks(
        tenant_id,
        document_id,
        ingestion_run_id=retry_run_id,
        normalized_audio=normalized,
        duration_ms=15_000,
        settings=settings,
        gateway=retry_gateway,  # type: ignore[arg-type]
        workdir=tmp_path,
    )
    assert retry == first
    assert retry_gateway.transcribe_calls == []
    assert extracted == [0, 1], "checkpoint reuse never re-extracts or re-bills a chunk"

    async with db_session.session_scope() as session:
        checkpoints = await TranscriptionCheckpointRepository(session, tenant_id).list_for_document(
            document_id, model=settings.transcription_model
        )
        retired = await TranscriptionCheckpointRepository(session, tenant_id).list_for_document(
            document_id, model="retired/stt-route"
        )
        document = await DocumentRepository(session, tenant_id).get(document_id)
    assert [(item.chunk_index, item.start_ms, item.end_ms) for item in checkpoints] == [
        (0, 0, 10_000),
        (1, 9_000, 15_000),
    ]
    assert retired == []
    assert document is not None
    assert document.status is DocumentStatus.PROCESSING
    assert document.updated_at >= document.created_at


async def test_persisted_media_result_has_timestamp_pairs_and_exactly_one_audit(
    sqlite_engine: None,
) -> None:
    settings = _settings()
    tenant_id, document_id = await _seed_media()
    segments = build_transcript_segments(
        (
            StitchedWord(
                text="My",
                start_ms=100,
                end_ms=250,
                speaker_id="speaker-1",
                confidence=0.99,
            ),
            StitchedWord(
                text="name",
                start_ms=260,
                end_ms=450,
                speaker_id="speaker-1",
                confidence=0.99,
            ),
            StitchedWord(
                text="is",
                start_ms=460,
                end_ms=550,
                speaker_id="speaker-1",
                confidence=0.99,
            ),
            StitchedWord(
                text="John.",
                start_ms=560,
                end_ms=800,
                speaker_id="speaker-1",
                confidence=0.99,
            ),
        )
    )
    identities = infer_speaker_names(segments)
    drafts = build_transcript_chunks(
        segments,
        identities,
        duration_ms=2_000,
        chunk_size=settings.ingestion_chunk_size,
        overlap=settings.ingestion_chunk_overlap,
    )
    embeddings = [Embedding(vector=[0.25] * 8, model="fake") for _ in drafts]

    async with db_session.session_scope() as session:
        before = await DocumentRepository(session, tenant_id).get(document_id)
    assert before is not None
    assert before.duration_ms is None, "first ingestion establishes the media time axis"

    first_run_id = uuid.uuid4()
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert (
            await DocumentRepository(session, tenant_id).claim_ingestion(
                document_id,
                ingestion_run_id=first_run_id,
                stale_before=datetime.now(UTC) - timedelta(minutes=30),
            )
            is not None
        )
    count = await ingest_module._persist_media_result(
        tenant_id,
        document_id,
        ingestion_run_id=first_run_id,
        kind=DocumentKind.AUDIO,
        duration_ms=2_000,
        language="en",
        segments=segments,
        chunk_drafts=drafts,
        embeddings=embeddings,
        settings=settings,
    )
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert (
            await DocumentRepository(session, tenant_id).finish_ingestion(
                document_id,
                ingestion_run_id=first_run_id,
                status=DocumentStatus.READY,
            )
            is not None
        )
    # Simulate the idempotent search-retry path: the locked document metadata
    # tells persistence that the transcribed audit already committed.
    retry_run_id = uuid.uuid4()
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert (
            await DocumentRepository(session, tenant_id).claim_ingestion(
                document_id,
                ingestion_run_id=retry_run_id,
                stale_before=datetime.now(UTC) - timedelta(minutes=30),
            )
            is not None
        )
    second_count = await ingest_module._persist_media_result(
        tenant_id,
        document_id,
        ingestion_run_id=retry_run_id,
        kind=DocumentKind.AUDIO,
        duration_ms=2_000,
        language="en",
        segments=segments,
        chunk_drafts=drafts,
        embeddings=embeddings,
        settings=settings,
    )
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert (
            await DocumentRepository(session, tenant_id).finish_ingestion(
                document_id,
                ingestion_run_id=retry_run_id,
                status=DocumentStatus.READY,
            )
            is not None
        )
    assert second_count == count

    async with db_session.session_scope() as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        transcript = await TranscriptRepository(session, tenant_id).list_segments(document_id)
        audit = await AuditEventRepository(session, tenant_id).list_recent()
    assert document is not None
    assert document.status is DocumentStatus.READY
    assert document.duration_ms == 2_000
    assert len(chunks) == count == len(drafts)
    assert len(transcript) == len(segments)
    assert all(
        chunk.time_start_ms is not None
        and chunk.time_end_ms is not None
        and 0 <= chunk.time_start_ms < chunk.time_end_ms <= document.duration_ms
        and (
            chunk.transcript_segment_id is None
            or chunk.transcript_segment_id in {segment.id for segment in transcript}
        )
        for chunk in chunks
    )
    transcribed = [event for event in audit if event.action == AuditAction.DOCUMENT_TRANSCRIBED]
    assert len(transcribed) == 1
    assert transcribed[0].metadata["model"] == settings.transcription_model


async def test_media_failure_terminalization_audits_exactly_once(sqlite_engine: None) -> None:
    tenant_id, document_id = await _seed_media()
    run_id = uuid.uuid4()
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert (
            await DocumentRepository(session, tenant_id).claim_ingestion(
                document_id,
                ingestion_run_id=run_id,
                stale_before=datetime.now(UTC) - timedelta(minutes=30),
            )
            is not None
        )

    first = await ingest_module._fail(
        tenant_id,
        document_id,
        "invalid media evidence",
        ingestion_run_id=run_id,
    )
    second = await ingest_module._fail(
        tenant_id,
        document_id,
        "invalid media evidence",
        ingestion_run_id=run_id,
    )

    assert first.status is DocumentStatus.FAILED
    assert second.status is DocumentStatus.FAILED
    async with db_session.session_scope() as session:
        events = await AuditEventRepository(session, tenant_id).list_recent()
    transcribed = [event for event in events if event.action == AuditAction.DOCUMENT_TRANSCRIBED]
    assert len(transcribed) == 1
    assert transcribed[0].outcome.value == "error"
    assert transcribed[0].metadata == {"reason": "media_ingestion_failed"}


async def test_simultaneous_media_deliveries_only_one_claimant_calls_stt(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh PROCESSING lease rejects a duplicate before any paid provider call."""
    settings = _settings(CONNECTOR_INGEST_RECOVERY_MINUTES="30")
    tenant_id, document_id = await _seed_media()
    entered = asyncio.Event()
    release = asyncio.Event()
    gateway = _Gateway(model=settings.transcription_model)

    async def _fake_ingest_media(
        claimed_tenant_id: uuid.UUID,
        claimed_document_id: uuid.UUID,
        *,
        ingestion_run_id: uuid.UUID,
        **_kwargs: object,
    ) -> ingest_module.IngestionResult:
        await gateway.transcribe(Path("paid-stt.wav"))
        call_number = len(gateway.transcribe_calls)
        if call_number == 1:
            entered.set()
            await release.wait()
        else:
            return ingest_module.IngestionResult(
                claimed_document_id, DocumentStatus.READY, chunk_count=0
            )
        del claimed_tenant_id, ingestion_run_id
        return ingest_module.IngestionResult(
            claimed_document_id, DocumentStatus.READY, chunk_count=0
        )

    async def _no_sync(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(ingest_module, "_ingest_media", _fake_ingest_media)
    monkeypatch.setattr(ingest_module, "_sync_index", _no_sync)

    first = asyncio.create_task(
        ingest_module.ingest_document_async(
            tenant_id,
            document_id,
            settings=settings,
            object_store=object(),  # type: ignore[arg-type]
            gateway=gateway,  # type: ignore[arg-type]
        )
    )
    await entered.wait()
    duplicate = await ingest_module.ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=object(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )
    release.set()
    winner = await first

    assert winner.status is DocumentStatus.READY
    assert duplicate.status is DocumentStatus.PROCESSING
    assert len(gateway.transcribe_calls) == 1


async def test_late_old_failure_cannot_regress_newer_ready_run(sqlite_engine: None) -> None:
    """A terminal write is a CAS on the durable run token, not just document id."""
    tenant_id, document_id = await _seed_media()
    old_run_id = uuid.uuid4()
    new_run_id = uuid.uuid4()
    normal_cutoff = datetime.now(UTC) - timedelta(minutes=30)

    async with db_session.tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        assert (
            await documents.claim_ingestion(
                document_id,
                ingestion_run_id=old_run_id,
                stale_before=normal_cutoff,
            )
            is not None
        )
    # A future cutoff deterministically models a lease older than the recovery
    # threshold without sleeping or reaching into ORM rows from the task test.
    async with db_session.tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        assert (
            await documents.claim_ingestion(
                document_id,
                ingestion_run_id=new_run_id,
                stale_before=datetime.now(UTC) + timedelta(seconds=1),
            )
            is not None
        )
        assert (
            await documents.finish_ingestion(
                document_id,
                ingestion_run_id=new_run_id,
                status=DocumentStatus.READY,
            )
            is not None
        )

    late = await ingest_module._fail(
        tenant_id,
        document_id,
        "old worker failed after takeover",
        ingestion_run_id=old_run_id,
    )

    assert late.status is DocumentStatus.READY
    async with db_session.tenant_session_scope(tenant_id) as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
    assert document is not None
    assert document.status is DocumentStatus.READY
    assert document.error is None


async def test_stale_takeover_fences_prior_worker_before_next_stt_call(
    sqlite_engine: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once a stale lease is replaced, its old token cannot heartbeat or spend."""
    settings = _settings(
        TRANSCRIPTION_CHUNK_SECONDS="10",
        TRANSCRIPTION_CHUNK_OVERLAP_SECONDS="1",
    )
    tenant_id, document_id = await _seed_media()
    old_run_id = uuid.uuid4()
    new_run_id = uuid.uuid4()

    async with db_session.tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        assert (
            await documents.claim_ingestion(
                document_id,
                ingestion_run_id=old_run_id,
                stale_before=datetime.now(UTC) - timedelta(minutes=30),
            )
            is not None
        )
    async with db_session.tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        assert (
            await documents.claim_ingestion(
                document_id,
                ingestion_run_id=new_run_id,
                stale_before=datetime.now(UTC) + timedelta(seconds=1),
            )
            is not None
        )
        assert await documents.touch_processing(document_id, old_run_id) is False
        assert await documents.touch_processing(document_id, new_run_id) is True

    normalized = tmp_path / "normalized.wav"
    normalized.write_bytes(b"audio")
    gateway = _Gateway(model=settings.transcription_model)

    async def _must_not_extract(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a fenced worker must stop before extracting or calling STT")

    monkeypatch.setattr(ingest_module, "extract_audio_chunk", _must_not_extract)
    with pytest.raises(ingest_module.IngestionLeaseLost):
        await ingest_module._transcribe_chunks(
            tenant_id,
            document_id,
            ingestion_run_id=old_run_id,
            normalized_audio=normalized,
            duration_ms=15_000,
            settings=settings,
            gateway=gateway,  # type: ignore[arg-type]
            workdir=tmp_path,
        )
    assert gateway.transcribe_calls == []
