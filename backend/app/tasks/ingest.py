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
6. **Advance status** ``pending → processing → ready`` and set ``chunk_count``;
   any parse/embed/persist failure marks the document ``failed`` with the reason
   (AC-6) and **does not crash silently**.
7. **Sync the search index** (ADR-0010 §5, dual-write): replace the document's
   chunk docs in OpenSearch via :func:`app.tasks.index_sync.sync_document_index_async`
   — retrieval serves from the engine (single-store), so ``ready`` must imply
   retrievable. An engine fault is a *transient* fault like storage/model: the
   run fails and Celery retries the (idempotent) pipeline as a unit.

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

from dataclasses import dataclass
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.core.errors import AppError, DependencyError
from app.db.repositories import ChunkInput, ChunkRepository, DocumentRepository
from app.db.session import tenant_session_scope
from app.domain.entities import DocumentStatus
from app.domain.llm import Embedding
from app.ingestion import DocumentParseError, chunk_text, parse_document
from app.llm import LLMGateway
from app.search import OpenSearchStore
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
            unavailable) — the document is left ``failed`` only by the Celery
            wrapper after retries are exhausted; this core re-raises so the
            wrapper can retry. A *permanent* parse failure is caught here and
            recorded as ``failed`` (returned, not raised) so it never retries.
    """
    # --- Phase 1: claim the document and move it to `processing`. ------------
    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        document = await documents.get(document_id)
        if document is None:
            # Nothing to do — the document was deleted (or never existed in this
            # tenant). Idempotent no-op, not an error.
            return IngestionResult(document_id, DocumentStatus.FAILED, 0, "document not found")
        storage_key = document.storage_key
        mime_type = document.mime_type
        await documents.set_status(document_id, DocumentStatus.PROCESSING, error=None)

    # --- Phase 2: fetch + parse + chunk + embed (outside the DB txn). --------
    # A parse failure is PERMANENT (corrupt/unsupported bytes) → fail the doc now
    # without retrying. A transient storage/model fault is RETRYABLE → re-raise.
    try:
        data = await object_store.get(str(tenant_id), storage_key)
    except AppError as exc:
        # Storage unavailable / object missing → retryable dependency fault.
        raise IngestionError(f"could not fetch document bytes: {exc.code}") from exc

    try:
        text = parse_document(data, mime_type=mime_type)
    except DocumentParseError as exc:
        return await _fail(tenant_id, document_id, str(exc))

    chunks = chunk_text(
        text,
        chunk_size=settings.ingestion_chunk_size,
        overlap=settings.ingestion_chunk_overlap,
    )

    if not chunks:
        # An empty/blank document parses to nothing — a valid, terminal outcome:
        # ready with zero chunks (idempotently clears any prior chunks).
        async with tenant_session_scope(tenant_id) as session:
            await ChunkRepository(session, tenant_id).replace_for_document(document_id, [])
            await DocumentRepository(session, tenant_id).set_status(
                document_id, DocumentStatus.READY, error=None
            )
        # Clear any prior chunks from the search index too (a re-ingest of a
        # now-empty document must not leave stale index entries — ADR-0010 §5).
        await _sync_index(tenant_id, document_id, settings=settings, store=search_store)
        return IngestionResult(document_id, DocumentStatus.READY, 0)

    try:
        embeddings = await _embed_in_batches(
            gateway,
            [c.text for c in chunks],
            batch_size=settings.ingestion_embed_batch_size,
        )
    except DependencyError as exc:
        # Model provider unavailable / unconfigured → retryable.
        raise IngestionError(f"could not embed chunks: {exc.code}") from exc

    if len(embeddings) != len(chunks):  # pragma: no cover — gateway contract guard
        raise IngestionError(f"embedding count {len(embeddings)} != chunk count {len(chunks)}")

    # --- Phase 3: persist chunks + mark ready (one transaction, idempotent). -
    chunk_inputs = [
        ChunkInput(
            text=chunk.text,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            embedding=embedding.vector,
        )
        for chunk, embedding in zip(chunks, embeddings, strict=True)
    ]
    async with tenant_session_scope(tenant_id) as session:
        persisted = await ChunkRepository(session, tenant_id).replace_for_document(
            document_id, chunk_inputs
        )
        await DocumentRepository(session, tenant_id).set_status(
            document_id, DocumentStatus.READY, error=None
        )

    # --- Phase 4: sync the search index (dual-write, ADR-0010 §5). -----------
    # Retrieval serves from the engine (single-store), so a document that
    # reports `ready` must be retrievable there. The sync is in-band and a
    # failure fails this (idempotent) run — the Celery wrapper retries the
    # whole pipeline as a unit; Postgres state is already durable and a re-run
    # replaces chunks + re-syncs, converging.
    await _sync_index(tenant_id, document_id, settings=settings, store=search_store)
    return IngestionResult(document_id, DocumentStatus.READY, len(persisted))


async def _sync_index(
    tenant_id: UUID,
    document_id: UUID,
    *,
    settings: Settings,
    store: OpenSearchStore | None,
) -> None:
    """Sync one document's index entries; translate engine faults to retryable.

    :class:`DependencyError` (engine unreachable / rejected) becomes
    :class:`IngestionError` so the Celery wrapper's existing transient-fault
    retry/backoff/dead-letter machinery applies unchanged.
    """
    try:
        await sync_document_index_async(
            tenant_id, document_id, settings=settings, store=store
        )
    except DependencyError as exc:
        raise IngestionError(f"could not index chunks: {exc.code}") from exc


async def _fail(tenant_id: UUID, document_id: UUID, reason: str) -> IngestionResult:
    """Mark a document ``failed`` with ``reason`` (own transaction). AC-6.

    A permanent failure: the reason is stored on the document row so a parse/embed
    fault is a recorded terminal state, never a silent drop. Tenant-scoped.
    """
    async with tenant_session_scope(tenant_id) as session:
        await DocumentRepository(session, tenant_id).set_status(
            document_id, DocumentStatus.FAILED, error=reason
        )
    return IngestionResult(document_id, DocumentStatus.FAILED, 0, reason)


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
    object_store = ObjectStore(settings)
    gateway = LLMGateway(settings)

    try:
        result = run_task(
            ingest_document_async(
                tid,
                did,
                settings=settings,
                object_store=object_store,
                gateway=gateway,
            )
        )
    except (IngestionError, DependencyError) as exc:
        # ``bind=True`` → ``self.request.retries`` is the 0-based attempt count.
        request = getattr(self, "request", None)
        retries: int = getattr(request, "retries", 0) or 0
        if retries >= settings.ingestion_max_retries:
            # Retries exhausted → dead-letter: record a permanent failed status
            # and acknowledge the message (return) rather than looping forever.
            result = run_task(_fail(tid, did, f"ingestion failed after {retries} retries: {exc}"))
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


def enqueue_ingestion(tenant_id: UUID, document_id: UUID) -> None:
    """Enqueue ingestion for an uploaded document (the seam #28 calls).

    The single enqueue point (ADR-0004: tasks are enqueued only from ``tasks/``).
    ``DocumentService.upload`` calls this (after-commit) once the ``pending`` row
    is durable so the worker drives ``pending → processing → ready/failed`` off
    the request path. Ids are passed as strings (Celery's JSON serializer).

    **Best-effort, bounded against the broker.** It runs after the upload has
    already committed and responded, so a transient broker outage must neither
    turn a successful upload into a 500 nor block the response indefinitely. The
    message is published on a connection whose reconnect is **bounded** (a couple
    of short attempts), so an unreachable broker raises
    ``kombu.exceptions.OperationalError`` in seconds rather than looping forever;
    that error is logged and swallowed — the document is left ``pending`` (a
    re-drive/sweep is out of scope, #21 fences). A *programming* error still
    propagates. This also keeps the upload API tests offline-safe: with no broker
    the publish fails fast and the document is created ``pending`` exactly as the
    tests assert.
    """
    from kombu.exceptions import OperationalError

    log = structlog.get_logger(__name__)
    try:
        # A bounded-reconnect connection so the publish fails fast on an
        # unreachable broker instead of Celery's default unbounded retry loop.
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
            ingest_document.apply_async(
                args=(str(tenant_id), str(document_id)),
                connection=connection,
                retry=False,
            )
    except OperationalError as exc:
        log.warning(
            "ingestion.enqueue_failed",
            document_id=str(document_id),
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )
