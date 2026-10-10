"""Document-ingestion Celery task — parse → chunk → embed → persist (CC-5 #21).

The async ingestion pipeline that turns an uploaded document (left
``status=pending`` by #28) into retrievable, embedded chunks, and the thin
Celery task that drives it. **The only place the ingestion task is defined or
enqueued** (ADR-0004: tasks live in ``tasks/``); ``DocumentService.upload``
enqueues it at the seam (#28's ``TODO(#21)``).

Pipeline (all slow/burst work — never the request path, backend/AGENTS.md):

1. **Fetch** the stored bytes via the #22 ``ObjectStore`` (the only object-store
   caller), tenant-prefix checked inside the adapter.
2. **Parse** by MIME type into plain text (:mod:`app.ingestion.parsers`), each
   parser library localized behind a helper.
3. **Chunk** into overlapping passages carrying exact ``char_start``/``char_end``
   offsets (:mod:`app.ingestion.chunking`) — the offsets back citations.
4. **Embed** each chunk via the merged LLM gateway ``embed()`` (the only model
   caller, #36), batched.
5. **Persist** the chunks (text + embedding vector + offsets + tenant + document
   + ordinal) via the #44 ``ChunkRepository``, tenant-scoped, **idempotently**
   (a re-run *replaces* the document's chunks — AC-5).
6. **Sync the search index** (ADR-0010 §5, dual-write): replace the document's
   chunk docs in OpenSearch via :func:`app.tasks.index_sync.sync_document_index_async`
   — retrieval serves from the engine (single-store), so ``ready`` must imply
   retrievable. An engine fault is a *transient* fault like storage/model: the
   run fails and Celery retries the (idempotent) pipeline as a unit.
7. **Activate ready** only after acknowledged publication, with the expected
   attempt CAS. Empty native extraction clears chunks and refreshed generations,
   then fails with an actionable message; it never publishes an empty Ready row.

Idempotency, retry-with-backoff, and dead-lettering (backend/AGENTS.md): the
task replaces (never duplicates) chunks; transient faults (storage/model/db
unavailable) retry with exponential backoff up to a cap, after which the document
is marked ``failed`` (the dead-letter — a permanent, queryable terminal state
rather than a lost message). A parse failure (corrupt/unsupported bytes) is
**permanent**: it fails the document immediately without burning retries.

Audit: ingestion is a system actor (no human in the loop); ``document.uploaded``
was already audited at upload (#28), so the task does not re-audit the upload —
it threads the system actor only where an audit event is appropriate.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

import structlog
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings, get_settings
from app.core.errors import AppError, DependencyError
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    DocumentRepository,
    TranscriptionCheckpointRepository,
    TranscriptRepository,
    TranscriptSegmentInput,
    TranscriptSpeakerInput,
)
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, DocumentKind, DocumentStatus, TranscriptionCheckpoint
from app.domain.llm import Embedding, Transcription, TranscriptionWord
from app.ingestion import DocumentParseError, chunk_text, parse_document
from app.ingestion.contract import ensure_embedding_contract, ingestion_enqueue_allowed
from app.ingestion.media import (
    AUDIO_MIME_TYPES,
    VIDEO_MIME_TYPES,
    ChunkTranscription,
    MediaProcessingError,
    TranscriptChunkDraft,
    TranscriptSegmentDraft,
    build_transcript_chunks,
    build_transcript_segments,
    extract_audio_chunk,
    infer_speaker_names,
    normalize_media_audio,
    plan_audio_chunks,
    probe_media,
    stitch_chunk_transcriptions,
)
from app.llm import InvalidTranscriptionResponse, LLMGateway
from app.search import OpenSearchStore
from app.services.audit import AuditSink
from app.storage import ObjectStore
from app.tasks.celery_app import celery_app
from app.tasks.index_sync import sync_document_index_async
from app.tasks.runner import run_task


class IngestionError(Exception):
    """A retryable ingestion fault (storage/model/db transiently unavailable).

    Distinct from :class:`~app.ingestion.parsers.DocumentParseError`, which is a
    *permanent* failure of the bytes themselves. The task retries on this and on
    :class:`~app.core.errors.DependencyError`; it fails the document immediately
    on a parse error (no point retrying corrupt input).
    """

    def __init__(
        self,
        detail: str,
        *,
        code: str = "ingestion_error",
        safe_message: str | None = None,
        attempt: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.safe_message = safe_message or detail
        self.attempt = attempt


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """The outcome of one ingestion run — what the task returns / the test asserts.

    ``status`` is the terminal document status (``ready`` or ``failed``);
    ``chunk_count`` is the number of chunks persisted (0 on failure or an
    empty/blank document); ``error`` carries the failure reason when failed.
    """

    document_id: UUID
    status: DocumentStatus
    chunk_count: int
    error: str | None = None


class IngestionLeaseLost(Exception):
    """The UUID checkpoint lease no longer belongs to this delivery."""


async def _current_ingestion_result(
    tenant_id: UUID, document_id: UUID, *, missing_error: str = "document not found"
) -> IngestionResult:
    """Return the durable state after this delivery lost/skipped its claim."""
    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        document = await documents.get(document_id)
        if document is None:
            return IngestionResult(document_id, DocumentStatus.FAILED, 0, missing_error)
        chunk_count = (
            await documents.count_chunks(document_id)
            if document.status is DocumentStatus.READY
            else 0
        )
        return IngestionResult(document_id, document.status, chunk_count, document.error)


async def _require_ingestion_lease(
    tenant_id: UUID, document_id: UUID, ingestion_run_id: UUID
) -> None:
    """Heartbeat one run token or stop immediately after takeover/terminalization."""
    async with tenant_session_scope(tenant_id) as session:
        held = await DocumentRepository(session, tenant_id).touch_processing(
            document_id, ingestion_run_id
        )
    if not held:
        raise IngestionLeaseLost(f"ingestion lease lost for document {document_id}")


async def _embed_in_batches(
    gateway: LLMGateway, texts: list[str], *, batch_size: int
) -> list[Embedding]:
    """Embed ``texts`` via the gateway in batches, preserving input order.

    Batching keeps provider round-trips down (one ``embed()`` call per batch).
    The gateway returns one :class:`Embedding` per input in order; concatenating
    the per-batch results preserves the overall chunk order. A blank inputs list
    yields no embeddings (the caller guards the empty-document case first).
    """
    embeddings: list[Embedding] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        embeddings.extend(await gateway.embed(batch))
    return embeddings


async def ingest_document_async(
    tenant_id: UUID,
    document_id: UUID,
    *,
    settings: Settings,
    object_store: ObjectStore,
    gateway: LLMGateway,
    search_store: OpenSearchStore | None = None,
    correlation_id: str | None = None,
    ingestion_run_id: UUID | None = None,
) -> IngestionResult:
    """Run the full ingestion pipeline for one document (the async core).

    Pure orchestration over the injected adapters (storage / model / db) so it is
    directly unit-testable with fakes offline — the Celery task is a thin sync
    wrapper around this. Tenant-scoped throughout (INV-1): the document, its
    object key, and its chunks are all read/written under ``tenant_id``.

    Status machine (AC-5/AC-6): ``pending → processing`` is committed first (so a
    crash mid-run leaves a visible ``processing``, not a stuck ``pending``), then
    on success ``processing → ready`` with ``chunk_count`` set, or on any failure
    ``→ failed`` with the recorded reason. Re-running replaces the document's
    chunks rather than duplicating them.

    Raises:
        IngestionError / DependencyError: a *transient* fault (storage/model/db
            unavailable) — this core records the attempt as ``failed`` in a fresh
            transaction, then re-raises so the wrapper can retry. A *permanent*
            parse failure is recorded as ``failed`` and returned, so it never
            retries.
    """
    run_id = ingestion_run_id or uuid4()
    stale_before = datetime.now(UTC) - timedelta(minutes=settings.connector_ingest_recovery_minutes)
    # --- Phase 1: claim the document and move it to `processing`. ------------
    try:
        async with tenant_session_scope(tenant_id) as session:
            documents = DocumentRepository(session, tenant_id)
            document = await documents.begin_ingestion(
                document_id, ingestion_run_id=run_id, stale_before=stale_before
            )
            if document is None:
                # Nothing to do — the document was deleted (or never existed in this
                # tenant). Idempotent no-op, not an error.
                return await _current_ingestion_result(tenant_id, document_id)
            storage_key = document.storage_key
            mime_type = document.mime_type
            attempt = document.ingestion_attempts
    except SQLAlchemyError as exc:
        raise IngestionError(
            "Document ingestion could not claim the database row.",
            code="ingestion_claim_database_error",
        ) from exc

    try:
        normalized_mime = mime_type.split(";", 1)[0].strip().lower()
        if normalized_mime in AUDIO_MIME_TYPES | VIDEO_MIME_TYPES:
            result = await _ingest_media(
                tenant_id,
                document_id,
                ingestion_run_id=run_id,
                storage_key=storage_key,
                mime_type=normalized_mime,
                expected_size_bytes=document.size_bytes,
                settings=settings,
                object_store=object_store,
                gateway=gateway,
            )
            published = await _sync_index(
                tenant_id,
                document_id,
                expected_attempt=attempt,
                settings=settings,
                store=search_store,
            )
            if not published:
                return await _current_ingestion_result(tenant_id, document_id)
            async with tenant_session_scope(tenant_id) as session:
                ready = await DocumentRepository(session, tenant_id).mark_ingestion_ready(
                    document_id,
                    expected_attempt=attempt,
                )
            if ready is None:
                await _discard_generation(
                    tenant_id,
                    document_id,
                    attempt=attempt,
                    settings=settings,
                    store=search_store,
                )
                return await _current_ingestion_result(tenant_id, document_id)
            return result
        return await _ingest_claimed_document(
            tenant_id,
            document_id,
            storage_key=storage_key,
            mime_type=mime_type,
            attempt=attempt,
            settings=settings,
            object_store=object_store,
            gateway=gateway,
            search_store=search_store,
            correlation_id=correlation_id,
        )
    except IngestionLeaseLost:
        return await _current_ingestion_result(tenant_id, document_id)
    except (MediaProcessingError, InvalidTranscriptionResponse, ValueError):
        return await _finalize_failure(
            tenant_id,
            document_id,
            "Media could not be validated or transcribed.",
            expected_attempt=attempt,
            code="media_ingestion_failed",
            correlation_id=correlation_id,
        )
    except IngestionError as exc:
        exc.attempt = attempt
        await _finalize_failure(
            tenant_id,
            document_id,
            exc.safe_message,
            expected_attempt=attempt,
            code=exc.code,
            correlation_id=correlation_id,
        )
        structlog.get_logger(__name__).warning(
            "ingestion.attempt_failed",
            tenant_id=str(tenant_id),
            document_id=str(document_id),
            attempt=attempt,
            failure_code=exc.code,
            correlation_id=correlation_id,
        )
        raise
    except SQLAlchemyError as exc:
        failure = IngestionError(
            "Document ingestion failed while saving chunks.",
            code="ingestion_database_error",
        )
        failure.attempt = attempt
        await _finalize_failure(
            tenant_id,
            document_id,
            failure.safe_message,
            expected_attempt=attempt,
            code=failure.code,
            correlation_id=correlation_id,
        )
        raise failure from exc
    except Exception as exc:  # noqa: BLE001 — terminal-state backstop
        failure = IngestionError(
            "Document ingestion failed unexpectedly.",
            code="ingestion_internal_error",
        )
        failure.attempt = attempt
        await _finalize_failure(
            tenant_id,
            document_id,
            failure.safe_message,
            expected_attempt=attempt,
            code=failure.code,
            correlation_id=correlation_id,
        )
        raise failure from exc


async def _ingest_claimed_document(
    tenant_id: UUID,
    document_id: UUID,
    *,
    storage_key: str,
    mime_type: str,
    attempt: int,
    settings: Settings,
    object_store: ObjectStore,
    gateway: LLMGateway,
    search_store: OpenSearchStore | None,
    correlation_id: str | None,
) -> IngestionResult:
    """Run phases after the durable claim; callers own terminal finalization."""

    try:
        data = await object_store.get(str(tenant_id), storage_key)
    except AppError as exc:
        raise IngestionError(
            "Document ingestion could not fetch the stored object.",
            code="ingestion_storage_error",
        ) from exc

    try:
        text = parse_document(data, mime_type=mime_type)
    except DocumentParseError as exc:
        return await _finalize_failure(
            tenant_id,
            document_id,
            str(exc),
            expected_attempt=attempt,
            code="document_parse_error",
            correlation_id=correlation_id,
        )

    chunks = chunk_text(
        text,
        chunk_size=settings.ingestion_chunk_size,
        overlap=settings.ingestion_chunk_overlap,
    )
    if not chunks:
        async with tenant_session_scope(tenant_id) as session:
            persisted = await ChunkRepository(session, tenant_id).replace_for_ingestion(
                document_id,
                [],
                expected_attempt=attempt,
                embedding_fingerprint=settings.embedding_space_fingerprint,
            )
        if persisted is None:
            return IngestionResult(document_id, DocumentStatus.FAILED, 0, "attempt superseded")
        published = await _sync_index(
            tenant_id,
            document_id,
            expected_attempt=attempt,
            settings=settings,
            store=search_store,
        )
        if not published:
            return IngestionResult(document_id, DocumentStatus.FAILED, 0, "attempt superseded")
        # Cleanup must acknowledge completion before this permanent failure is
        # finalized. A cleanup fault remains retryable; the attempt fence keeps
        # an old empty delivery from overwriting a newer successful ingestion.
        return await _finalize_failure(
            tenant_id,
            document_id,
            "No native text was extracted. Use a text-bearing file or an OCR-enabled workflow.",
            expected_attempt=attempt,
            code="no_native_text",
            correlation_id=correlation_id,
        )

    try:
        embeddings = await _embed_in_batches(
            gateway,
            [chunk.text for chunk in chunks],
            batch_size=settings.ingestion_embed_batch_size,
        )
    except DependencyError as exc:
        code = (
            exc.code
            if exc.code in {"embedding_dimension_mismatch", "embedding_count_mismatch"}
            else "ingestion_embedding_error"
        )
        raise IngestionError(
            "Document ingestion failed while creating embeddings.",
            code=code,
        ) from exc

    if len(embeddings) != len(chunks):
        raise IngestionError(
            "Document ingestion received an unexpected embedding count.",
            code="embedding_count_mismatch",
        )

    chunk_inputs = [
        ChunkInput(
            text=chunk.text,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            embedding=embedding.vector,
            embedding_fingerprint=settings.embedding_space_fingerprint,
        )
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]
    try:
        async with tenant_session_scope(tenant_id) as session:
            persisted = await ChunkRepository(session, tenant_id).replace_for_ingestion(
                document_id,
                chunk_inputs,
                expected_attempt=attempt,
                embedding_fingerprint=settings.embedding_space_fingerprint,
            )
    except ValueError as exc:
        raise IngestionError(
            "Document re-embedding would change legacy chunk boundaries.",
            code="legacy_chunk_shape_mismatch",
        ) from exc

    if persisted is None:
        return IngestionResult(document_id, DocumentStatus.FAILED, 0, "attempt superseded")
    published = await _sync_index(
        tenant_id,
        document_id,
        expected_attempt=attempt,
        settings=settings,
        store=search_store,
    )
    if not published:
        return IngestionResult(document_id, DocumentStatus.FAILED, 0, "attempt superseded")
    async with tenant_session_scope(tenant_id) as session:
        ready = await DocumentRepository(session, tenant_id).mark_ingestion_ready(
            document_id, expected_attempt=attempt
        )
    if ready is None:
        await _discard_generation(
            tenant_id,
            document_id,
            attempt=attempt,
            settings=settings,
            store=search_store,
        )
        return IngestionResult(document_id, DocumentStatus.FAILED, 0, "attempt superseded")
    return IngestionResult(document_id, DocumentStatus.READY, len(persisted))


def _checkpoint_result(checkpoint: TranscriptionCheckpoint) -> Transcription:
    """Rehydrate a normalized paid-result checkpoint, validating fail-closed."""
    words: list[TranscriptionWord] = []
    previous_start = -1
    for raw in checkpoint.words:
        text = raw.get("text")
        start = raw.get("start_ms")
        end = raw.get("end_ms")
        speaker = raw.get("speaker_label")
        confidence = raw.get("confidence")
        if (
            not isinstance(text, str)
            or not text.strip()
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < previous_start
            or start < 0
            or end <= start
            or not isinstance(speaker, str)
            or not speaker.strip()
            or (
                confidence is not None
                and (
                    isinstance(confidence, bool)
                    or not isinstance(confidence, int | float)
                    or not 0 <= float(confidence) <= 1
                )
            )
        ):
            raise MediaProcessingError("stored transcription checkpoint is invalid")
        words.append(
            TranscriptionWord(
                text=text.strip(),
                start_ms=start,
                end_ms=end,
                speaker_label=speaker.strip(),
                confidence=float(confidence) if confidence is not None else None,
            )
        )
        previous_start = start
    if not words:
        raise MediaProcessingError("stored transcription checkpoint is empty")
    return Transcription(
        text=" ".join(word.text for word in words),
        words=tuple(words),
        language=checkpoint.language,
        model=checkpoint.model,
    )


def _checkpoint_words(result: Transcription) -> list[dict[str, object]]:
    return [
        {
            "text": word.text,
            "start_ms": word.start_ms,
            "end_ms": word.end_ms,
            "speaker_label": word.speaker_label,
            "confidence": word.confidence,
        }
        for word in result.words
    ]


def _detected_language(results: list[ChunkTranscription]) -> str | None:
    values = [
        result.result.language.strip()
        for result in results
        if result.result.language is not None and result.result.language.strip()
    ]
    if not values:
        return None
    folded = Counter(value.casefold() for value in values)
    winner = folded.most_common(1)[0][0]
    return next(value for value in values if value.casefold() == winner)


async def _transcribe_chunks(
    tenant_id: UUID,
    document_id: UUID,
    *,
    ingestion_run_id: UUID,
    normalized_audio: Path,
    duration_ms: int,
    settings: Settings,
    gateway: LLMGateway,
    workdir: Path,
) -> list[ChunkTranscription]:
    spans = plan_audio_chunks(
        duration_ms=duration_ms,
        max_chunk_ms=settings.transcription_chunk_seconds * 1000,
        overlap_ms=settings.transcription_chunk_overlap_seconds * 1000,
    )
    # Checkpoints are provenance-bound to a model route. Keep only the active
    # route so a model/config change cannot leave ambiguous paid evidence for a
    # later recovery or operator inspection.
    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        if not await documents.touch_processing(document_id, ingestion_run_id):
            raise IngestionLeaseLost
        await TranscriptionCheckpointRepository(session, tenant_id).delete_other_models(
            document_id,
            keep_model=settings.transcription_model,
            ingestion_run_id=ingestion_run_id,
        )
    results: list[ChunkTranscription] = []
    for span in spans:
        checkpoint: TranscriptionCheckpoint | None
        async with tenant_session_scope(tenant_id) as session:
            documents = DocumentRepository(session, tenant_id)
            if not await documents.touch_processing(document_id, ingestion_run_id):
                raise IngestionLeaseLost
            checkpoint = await TranscriptionCheckpointRepository(session, tenant_id).get(
                document_id,
                chunk_index=span.index,
                model=settings.transcription_model,
            )
        if (
            checkpoint is not None
            and checkpoint.start_ms == span.start_ms
            and checkpoint.end_ms == span.end_ms
        ):
            results.append(ChunkTranscription(span=span, result=_checkpoint_result(checkpoint)))
            continue

        chunk_path = workdir / f"chunk-{span.index:05d}.wav"
        await extract_audio_chunk(
            normalized_audio,
            chunk_path,
            span=span,
            ffmpeg_path=settings.ffmpeg_path,
        )
        # A stale worker that resumed during extraction must re-prove ownership
        # immediately before the paid provider boundary.
        await _require_ingestion_lease(tenant_id, document_id, ingestion_run_id)
        result = await gateway.transcribe(chunk_path)
        if result.model != settings.transcription_model:
            raise MediaProcessingError("transcription result model provenance is inconsistent")

        # Persist the normalized paid output BEFORE embedding/indexing. A task
        # retry resumes here without rebilling completed provider chunks.
        async with tenant_session_scope(tenant_id) as session:
            documents = DocumentRepository(session, tenant_id)
            if not await documents.touch_processing(document_id, ingestion_run_id):
                raise IngestionLeaseLost
            stored = await TranscriptionCheckpointRepository(session, tenant_id).upsert(
                document_id,
                ingestion_run_id=ingestion_run_id,
                chunk_index=span.index,
                model=result.model,
                start_ms=span.start_ms,
                end_ms=span.end_ms,
                language=result.language,
                words=_checkpoint_words(result),
            )
            if stored is None:
                raise MediaProcessingError("document disappeared during transcription")
            # The global stranded-task sweep includes PROCESSING rows. Refresh
            # the lease after every <=10-minute paid chunk so long media is not
            # mistaken for a dead worker and published concurrently.
        results.append(ChunkTranscription(span=span, result=result))
        # Cleanup happens only AFTER the paid normalized result committed. If
        # local file deletion itself fails and retries the task, this checkpoint
        # is reused and the provider is not called a second time.
        chunk_path.unlink(missing_ok=True)
    return results


async def _persist_media_result(
    tenant_id: UUID,
    document_id: UUID,
    *,
    ingestion_run_id: UUID,
    kind: DocumentKind,
    duration_ms: int,
    language: str | None,
    segments: tuple[TranscriptSegmentDraft, ...],
    chunk_drafts: tuple[TranscriptChunkDraft, ...],
    embeddings: list[Embedding],
    settings: Settings,
) -> int:
    identities = infer_speaker_names(segments)
    if len(embeddings) != len(chunk_drafts):
        raise IngestionError(
            f"embedding count {len(embeddings)} != media chunk count {len(chunk_drafts)}"
        )
    if any(len(embedding.vector) != settings.llm_embedding_dimensions for embedding in embeddings):
        raise IngestionError(
            "Media ingestion received an unexpected embedding dimension.",
            code="embedding_dimension_mismatch",
        )
    segment_ids = {segment.id for segment in segments}
    if any(
        draft.transcript_segment_id is not None and draft.transcript_segment_id not in segment_ids
        for draft in chunk_drafts
    ):
        raise MediaProcessingError("media chunk references a foreign transcript segment")

    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        locked_document = await documents.get_claimed_for_update(document_id, ingestion_run_id)
        if locked_document is None:
            raise IngestionLeaseLost
        was_transcribed = locked_document.transcription_model is not None
        # Transcript repository validation is duration-relative. Establish the
        # validated media axis first inside this same transaction; status stays
        # PROCESSING through transcript/chunk persistence and derived-index sync.
        updated = await documents.update_media_metadata(
            document_id,
            kind=kind,
            duration_ms=duration_ms,
            transcript_language=language,
            transcription_model=settings.transcription_model,
            ingestion_run_id=ingestion_run_id,
        )
        if updated is None:
            raise MediaProcessingError("document disappeared while persisting transcript")
        transcript = TranscriptRepository(session, tenant_id)
        speakers, persisted_segments = await transcript.replace_for_document(
            document_id,
            speakers=[
                TranscriptSpeakerInput(
                    speaker_id=identity.speaker_id,
                    display_name=identity.display_name,
                    name_status=identity.status.value,
                    name_confidence=identity.confidence,
                    name_method=identity.method.value if identity.method is not None else None,
                    evidence_segment_ids=identity.evidence_segment_ids,
                )
                for identity in identities
            ],
            segments=[
                TranscriptSegmentInput(
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
                for segment in segments
            ],
        )
        if len(persisted_segments) != len(segments) or len(speakers) != len(identities):
            raise MediaProcessingError("media transcript could not be persisted")

        persisted_chunks = await ChunkRepository(session, tenant_id).replace_for_ingestion(
            document_id,
            [
                ChunkInput(
                    text=draft.text,
                    char_start=draft.char_start,
                    char_end=draft.char_end,
                    embedding=embedding.vector,
                    embedding_fingerprint=settings.embedding_space_fingerprint,
                    time_start_ms=draft.time_start_ms,
                    time_end_ms=draft.time_end_ms,
                    transcript_segment_id=draft.transcript_segment_id,
                    speaker_id=draft.speaker_id,
                    speaker_name=draft.speaker_name,
                )
                for draft, embedding in zip(chunk_drafts, embeddings, strict=True)
            ],
            expected_attempt=locked_document.ingestion_attempts,
            embedding_fingerprint=settings.embedding_space_fingerprint,
        )
        if persisted_chunks is None:
            raise IngestionLeaseLost

        if not was_transcribed:
            await AuditSink(AuditEventRepository(session, tenant_id)).emit(
                action=AuditAction.DOCUMENT_TRANSCRIBED,
                actor=AuditActor.system(),
                resource_type="document",
                resource_id=str(document_id),
                outcome=AuditOutcome.ALLOWED,
                request_id="document-ingestion-task",
                source_ip="system",
                metadata={
                    "model": settings.transcription_model,
                    "language": language,
                    "duration_ms": duration_ms,
                    "speaker_count": len(speakers),
                    "segment_count": len(persisted_segments),
                },
            )
    return len(persisted_chunks)


async def _ingest_media(
    tenant_id: UUID,
    document_id: UUID,
    *,
    ingestion_run_id: UUID,
    storage_key: str,
    mime_type: str,
    expected_size_bytes: int,
    settings: Settings,
    object_store: ObjectStore,
    gateway: LLMGateway,
) -> IngestionResult:
    """Stream, validate, transcribe, embed, and persist under one fenced lease."""

    async def _heartbeat() -> None:
        interval = max(1, settings.connector_ingest_recovery_minutes * 60 // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await _require_ingestion_lease(tenant_id, document_id, ingestion_run_id)
            except IngestionLeaseLost:
                raise
            except Exception as exc:  # noqa: BLE001 - worker dependency boundary
                raise IngestionError("media ingestion heartbeat failed") from exc

    async def _pipeline() -> int:
        with TemporaryDirectory(prefix="lumen-media-") as raw_workdir:
            workdir = Path(raw_workdir)
            original = workdir / "original.media"
            metadata = await object_store.download_to_path(str(tenant_id), storage_key, original)
            actual_size = original.stat().st_size
            if actual_size != expected_size_bytes or metadata.size_bytes != expected_size_bytes:
                raise MediaProcessingError("stored media size does not match upload metadata")
            probe = await probe_media(
                original,
                mime_type=mime_type,
                ffprobe_path=settings.ffprobe_path,
                max_duration_ms=settings.media_max_duration_seconds * 1000,
            )
            normalized = workdir / "normalized.wav"
            await normalize_media_audio(
                original,
                normalized,
                probe=probe,
                ffmpeg_path=settings.ffmpeg_path,
            )
            results = await _transcribe_chunks(
                tenant_id,
                document_id,
                ingestion_run_id=ingestion_run_id,
                normalized_audio=normalized,
                duration_ms=probe.duration_ms,
                settings=settings,
                gateway=gateway,
                workdir=workdir,
            )
            stitched = stitch_chunk_transcriptions(results, duration_ms=probe.duration_ms)
            segments = build_transcript_segments(stitched)
            identities = infer_speaker_names(segments)
            chunk_drafts = build_transcript_chunks(
                segments,
                identities,
                duration_ms=probe.duration_ms,
                chunk_size=settings.ingestion_chunk_size,
                overlap=settings.ingestion_chunk_overlap,
            )
            try:
                embeddings = await _embed_in_batches(
                    gateway,
                    [chunk.text for chunk in chunk_drafts],
                    batch_size=settings.ingestion_embed_batch_size,
                )
            except DependencyError as exc:
                raise IngestionError(
                    "Media ingestion failed while creating embeddings.",
                    code=exc.code
                    if exc.code in {"embedding_dimension_mismatch", "embedding_count_mismatch"}
                    else "ingestion_embedding_error",
                ) from exc
            return await _persist_media_result(
                tenant_id,
                document_id,
                ingestion_run_id=ingestion_run_id,
                kind=DocumentKind(probe.kind),
                duration_ms=probe.duration_ms,
                language=_detected_language(results),
                segments=segments,
                chunk_drafts=chunk_drafts,
                embeddings=embeddings,
                settings=settings,
            )

    try:
        heartbeat = asyncio.create_task(_heartbeat())
        pipeline = asyncio.create_task(_pipeline())
        done, _pending = await asyncio.wait(
            (pipeline, heartbeat), return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat in done:
            pipeline.cancel()
            try:
                await pipeline
            except asyncio.CancelledError:
                pass
            await heartbeat  # raises the lease/dependency fault
            raise AssertionError("ingestion heartbeat stopped without an error")
        try:
            count = await pipeline
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
    except AppError as exc:
        raise IngestionError(f"media dependency failed: {exc.code}") from exc
    except OSError as exc:
        # Temporary-disk/file I/O is worker weather, not evidence that the
        # uploaded media is permanently invalid. Keep paths/details opaque and
        # let Celery's bounded backoff retry on a fresh work directory.
        raise IngestionError("media worker file I/O failed") from exc
    return IngestionResult(document_id, DocumentStatus.READY, count)


async def _sync_index(
    tenant_id: UUID,
    document_id: UUID,
    *,
    expected_attempt: int,
    settings: Settings,
    store: OpenSearchStore | None,
) -> bool:
    """Sync one document's index entries; translate engine faults to retryable.

    :class:`DependencyError` (engine unreachable / rejected) becomes
    :class:`IngestionError` so the Celery wrapper's existing transient-fault
    retry/backoff/dead-letter machinery applies unchanged.
    """
    try:
        result = await sync_document_index_async(
            tenant_id,
            document_id,
            expected_attempt=expected_attempt,
            settings=settings,
            store=store,
            refresh=True,
            require_search_visibility=True,
        )
        return not result.superseded
    except DependencyError as exc:
        code = (
            exc.code
            if exc.code in {"embedding_dimension_mismatch", "embedding_space_mismatch"}
            else "ingestion_index_error"
        )
        raise IngestionError(
            "Document ingestion failed while updating the search index.",
            code=code,
        ) from exc


async def _fail(
    tenant_id: UUID,
    document_id: UUID,
    reason: str,
    *,
    expected_attempt: int | None = None,
    code: str = "ingestion_retries_exhausted",
    correlation_id: str | None = None,
    ingestion_run_id: UUID | None = None,
) -> IngestionResult:
    """Mark a document ``failed`` with ``reason`` (own transaction). AC-6.

    A permanent failure: the reason is stored on the document row so a parse/embed
    fault is a recorded terminal state, never a silent drop. Tenant-scoped.
    """
    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        if ingestion_run_id is not None:
            claimed = await documents.get_claimed_for_update(document_id, ingestion_run_id)
            if claimed is None:
                return await _current_ingestion_result(tenant_id, document_id)
            expected_attempt = claimed.ingestion_attempts
        document = await documents.mark_ingestion_failed(
            document_id,
            expected_attempt=expected_attempt,
            code=code,
            message=reason,
            correlation_id=correlation_id,
        )
        if (
            document is not None
            and document.transcription_model is None
            and (
                document.mime_type.split(";", 1)[0].strip().lower()
                in AUDIO_MIME_TYPES | VIDEO_MIME_TYPES
            )
        ):
            await AuditSink(AuditEventRepository(session, tenant_id)).emit(
                action=AuditAction.DOCUMENT_TRANSCRIBED,
                actor=AuditActor.system(),
                resource_type="document",
                resource_id=str(document_id),
                outcome=AuditOutcome.ERROR,
                request_id="document-ingestion-task",
                source_ip="system",
                metadata={"reason": "media_ingestion_failed"},
            )
    if document is None:
        return await _current_ingestion_result(tenant_id, document_id)
    return IngestionResult(document_id, DocumentStatus.FAILED, 0, reason)


async def _finalize_failure(
    tenant_id: UUID,
    document_id: UUID,
    reason: str,
    *,
    expected_attempt: int,
    code: str,
    correlation_id: str | None = None,
) -> IngestionResult:
    """Boundedly recover a transient finalizer transaction failure (R1-003)."""

    for finalizer_attempt in range(2):
        try:
            return await _fail(
                tenant_id,
                document_id,
                reason,
                expected_attempt=expected_attempt,
                code=code,
                correlation_id=correlation_id,
            )
        except SQLAlchemyError as exc:
            if finalizer_attempt == 1:
                raise IngestionError(
                    "Document ingestion could not finalize its terminal state.",
                    code="ingestion_finalize_database_error",
                    attempt=expected_attempt,
                ) from exc
    raise AssertionError("unreachable")


async def _discard_generation(
    tenant_id: UUID,
    document_id: UUID,
    *,
    attempt: int,
    settings: Settings,
    store: OpenSearchStore | None,
) -> None:
    """Best-effort exact-generation cleanup; DB status remains the visibility gate."""

    owns_store = store is None
    active = store or OpenSearchStore.from_settings(settings)
    try:
        await active.delete_document_generation(
            tenant_id=tenant_id,
            document_id=document_id,
            ingestion_attempt=attempt,
        )
    except DependencyError as exc:
        structlog.get_logger(__name__).warning(
            "ingestion.generation_cleanup_deferred",
            tenant_id=str(tenant_id),
            document_id=str(document_id),
            attempt=attempt,
            failure_code=exc.code,
        )
    finally:
        if owns_store:
            await active.aclose()


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.ingest_document",
    bind=True,
    acks_late=True,
    max_retries=None,  # the effective cap comes from settings.ingestion_max_retries
)
def ingest_document(self: object, tenant_id: str, document_id: str) -> dict[str, object]:
    """Celery entrypoint: ingest one uploaded document (the sync wrapper).

    Resolves config + adapters, runs :func:`ingest_document_async` on a fresh
    event loop via :func:`app.tasks.runner.run_task` (which disposes the DB engine
    after each run so no pooled connection outlives its loop, #140), and translates
    its outcome to Celery's retry/terminal semantics:

    * **success** → return the result dict (status ``ready``, chunk count);
    * **transient fault** (``IngestionError``/``DependencyError``) → ``self.retry``
      with exponential backoff up to ``ingestion_max_retries``; on the final
      attempt mark the document ``failed`` (the dead-letter terminal state) and
      return rather than raising, so the message is acknowledged not redelivered
      forever;
    * **permanent parse fault** → already recorded ``failed`` by the async core,
      returned as a normal (non-retried) result.

    Args are strings (Celery serializes JSON, not UUIDs); they are parsed back to
    ``UUID`` here. Never crashes silently: every path ends in a stored status.
    """
    settings = get_settings()
    tid = UUID(tenant_id)
    did = UUID(document_id)
    run_id = uuid4()
    object_store = ObjectStore(settings)
    gateway = LLMGateway(settings)
    request = getattr(self, "request", None)
    correlation_id = getattr(request, "id", None)

    async def _run() -> IngestionResult:
        await ensure_embedding_contract(settings)
        return await ingest_document_async(
            tid,
            did,
            settings=settings,
            object_store=object_store,
            gateway=gateway,
            correlation_id=correlation_id,
            ingestion_run_id=run_id,
        )

    try:
        result = run_task(_run())
    except (IngestionError, DependencyError) as exc:
        # ``bind=True`` → ``self.request.retries`` is the 0-based attempt count.
        retries: int = getattr(request, "retries", 0) or 0
        if retries >= settings.ingestion_max_retries:
            # Retries exhausted → dead-letter: record a permanent failed status
            # and acknowledge the message (return) rather than looping forever.
            reason = f"Document ingestion failed after {retries} retries."
            failure_kwargs: dict[str, object] = {}
            if correlation_id:
                failure_kwargs["correlation_id"] = correlation_id
            exhausted_attempt = getattr(exc, "attempt", None)
            preclaim_failure = isinstance(exc, DependencyError) or getattr(exc, "code", None) in {
                "ingestion_claim_database_error",
                "embedding_contract_preflight_failed",
                "embedding_contract_unvalidated",
            }
            if exhausted_attempt is None and preclaim_failure:
                # Startup/worker compatibility failed before a DB claim. Keep
                # the durable row pending for the stranded-work sweep instead
                # of fabricating a terminal state owned by no generation.
                return {
                    "document_id": str(did),
                    "status": DocumentStatus.PENDING.value,
                    "chunk_count": 0,
                    "error": "Embedding contract is not ready; ingestion deferred.",
                }
            if exhausted_attempt is not None:
                failure_kwargs["expected_attempt"] = exhausted_attempt
            else:
                failure_kwargs["ingestion_run_id"] = run_id
            failure = _fail(tid, did, reason, **failure_kwargs)  # type: ignore[arg-type]
            try:
                result = run_task(failure)
            except SQLAlchemyError as finalize_exc:
                # Two bounded broker redeliveries beyond the ordinary work
                # budget are reserved for terminal DB publication. If the DB
                # remains unavailable, acknowledge with an explicitly deferred
                # state; the stranded-processing sweep owns recovery.
                finalizer_retry_ceiling = settings.ingestion_max_retries + 2
                if retries < finalizer_retry_ceiling:
                    countdown = settings.ingestion_retry_backoff_seconds * (2**retries)
                    raise self.retry(  # type: ignore[attr-defined]
                        exc=IngestionError(
                            "Document ingestion terminal state is awaiting the database.",
                            code="ingestion_finalize_database_error",
                            attempt=exhausted_attempt,
                        ),
                        countdown=countdown,
                    ) from finalize_exc
                return {
                    "document_id": str(did),
                    "status": DocumentStatus.PROCESSING.value,
                    "chunk_count": 0,
                    "error": "Terminal state deferred to stranded-work recovery.",
                }
            return _as_dict(result)
        # Exponential backoff: base * 2**retries.
        countdown = settings.ingestion_retry_backoff_seconds * (2**retries)
        # ``self.retry`` raises Celery's ``Retry`` to reschedule; chain the cause.
        raise self.retry(exc=exc, countdown=countdown) from exc  # type: ignore[attr-defined]

    return _as_dict(result)


def _as_dict(result: IngestionResult) -> dict[str, object]:
    """Render an :class:`IngestionResult` as the JSON-able task return value."""
    return {
        "document_id": str(result.document_id),
        "status": result.status.value,
        "chunk_count": result.chunk_count,
        "error": result.error,
    }


def enqueue_ingestion(tenant_id: UUID, document_id: UUID, *, media: bool = False) -> bool:
    """Enqueue ingestion for an uploaded document (the seam #28 calls).

    The single enqueue point (ADR-0004: tasks are enqueued only from ``tasks/``).
    ``DocumentService.upload`` calls this (after-commit) once the ``pending`` row
    is durable so the worker drives ``pending → processing → ready/failed`` off
    the request path. Ids are passed as strings (Celery's JSON serializer).

    **Best-effort, bounded against the broker.** It runs after the upload/source
    row has committed, so a transient broker outage must neither
    turn a successful upload into a 500 nor block the response indefinitely. The
    message is published on a connection whose reconnect is **bounded** (a couple
    of short attempts), so an unreachable broker raises
    ``kombu.exceptions.OperationalError`` in seconds rather than looping forever;
    that error is logged and swallowed — the document is left ``pending`` for
    the bounded stranded-work sweep to re-drive. A *programming* error still
    propagates. This also keeps the upload API tests offline-safe: with no broker
    the publish fails fast and the document is created ``pending`` exactly as the
    tests assert.
    """
    from kombu.exceptions import OperationalError

    log = structlog.get_logger(__name__)
    settings = get_settings()
    if not ingestion_enqueue_allowed(settings.embedding_space_fingerprint):
        log.warning(
            "ingestion.enqueue_deferred_contract",
            document_id=str(document_id),
            tenant_id=str(tenant_id),
        )
        return False
    try:
        # A bounded-reconnect connection so the publish fails fast on an
        # unreachable broker instead of Celery's default unbounded retry loop.
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
            ingest_document.apply_async(
                args=(str(tenant_id), str(document_id)),
                connection=connection,
                queue="media-ingestion" if media else "celery",
                retry=False,
            )
        return True
    except OperationalError as exc:
        log.warning(
            "ingestion.enqueue_failed",
            document_id=str(document_id),
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )
        return False
