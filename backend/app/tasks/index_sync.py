"""Search-index synchronization — make the engine match Postgres (ADR-0010 §5).

The **one** write-path operation of the single-retrieval-store migration (epic
#189, slice 2): ``sync_document_index_async(tenant_id, document_id)`` reads the
document's current state from Postgres — the source of truth — and makes the
OpenSearch index agree with it:

* document exists with chunks → **replace** its chunk docs in the index
  (delete-then-bulk-upsert, so stale chunk ids from a prior ingest vanish);
* document gone, or has no chunks → **delete** its chunk docs from the index.

Because the operation is *sync-to-source-of-truth* rather than an event apply,
it is idempotent by construction: re-running it (a Celery retry, a backfill, a
crash re-drive) converges on the same index state. Every mutation path reuses
it — the ingestion pipeline calls the async core in-process; request-path
deletions (documents / collections / sources services) enqueue the thin Celery
task **after commit** via :func:`enqueue_index_sync` (the single enqueue point,
ADR-0004); the connector re-sync calls the core directly for stale documents.

**Postgres stays authoritative.** An index write failure never mutates or rolls
back relational state: the Celery wrapper retries with backoff and, when
retries are exhausted, logs the inconsistency and acknowledges — the index is
derived and repairable (``python -m app.search.reindex``), so a dead engine
must not poison the queue. The ingestion pipeline is stricter: there the sync
runs in-band and a failure fails the (idempotent) ingest run, because a
document that reports ``ready`` should be retrievable (ADR-0010 single-store —
retrieval serves from the engine).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.core.errors import DependencyError
from app.db.repositories import ChunkRepository, DocumentRepository
from app.db.session import tenant_session_scope
from app.domain.entities import Chunk, Document, DocumentStatus
from app.search import IndexedChunk, OpenSearchStore
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task

log = structlog.get_logger(__name__)

# Documents per attestation update-by-query — bounds the request body when a
# whole source's corpus is re-attested by one complete replay.
_ATTEST_BATCH_SIZE = 500


@dataclass(frozen=True, slots=True)
class IndexSyncResult:
    """The outcome of one index sync — what the task returns / tests assert."""

    document_id: UUID
    indexed_count: int
    deleted: bool
    superseded: bool = False


def _to_indexed(
    document: Document,
    chunks: list[Chunk],
    *,
    embedding_fingerprint: str,
) -> list[IndexedChunk]:
    """Project db rows into the engine's write shape (ids + text + vector + spans).

    The four ACL-mirror fields (ADR-0019 §2) ride along from the document row —
    Postgres is authoritative, so a re-sync/reindex always converges the engine
    onto the current mirrored state (incl. a stale-stamped ``acl_synced_at``).
    """
    if any(chunk.embedding_fingerprint != embedding_fingerprint for chunk in chunks):
        raise DependencyError(
            "Stored chunks belong to another embedding coordinate space.",
            code="embedding_space_mismatch",
        )
    return [
        IndexedChunk(
            chunk_id=chunk.id,
            tenant_id=chunk.tenant_id,
            document_id=chunk.document_id,
            owner_id=document.owner_id,
            collection_id=document.collection_id,
            ord=chunk.ord,
            text=chunk.text,
            embedding=chunk.embedding,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            ingestion_attempt=document.ingestion_attempts,
            embedding_fingerprint=chunk.embedding_fingerprint or "",
            acl_enforced=document.acl_enforced,
            acl_principals=document.acl_principals or (),
            acl_synced_at=document.acl_synced_at,
            acl_scope_ids=document.acl_scope_ids or (),
        )
        for chunk in chunks
    ]


async def sync_document_index_async(
    tenant_id: UUID,
    document_id: UUID,
    *,
    settings: Settings,
    store: OpenSearchStore | None = None,
    refresh: bool = False,
    expected_attempt: int | None = None,
) -> IndexSyncResult:
    """Make the search index match Postgres for one document (idempotent).

    Reads the document + its chunks under the tenant session (INV-1), then
    replaces (or removes) the document's chunk docs in the engine. The delete
    always runs first so chunk ids from a prior ingest — ``replace_for_document``
    mints new ids every run — never linger as orphans.

    ``store`` is injectable for tests/backfill; when omitted the store is built
    from settings and closed here. ``refresh=True`` makes the result immediately
    searchable — for tests only; production paths leave the engine's refresh
    cadence alone.

    Raises:
        DependencyError: the engine is unreachable or rejected the write. The
            caller decides the policy — the ingestion pipeline fails its run
            (retry as a unit); the Celery wrapper retries then logs-and-acks.
    """
    async with tenant_session_scope(tenant_id) as session:
        document = await DocumentRepository(session, tenant_id).get(document_id)
        if expected_attempt is not None and (
            document is None
            or document.status is not DocumentStatus.PROCESSING
            or document.ingestion_attempts != expected_attempt
        ):
            return IndexSyncResult(document_id, 0, deleted=False, superseded=True)
        chunks = (
            await ChunkRepository(session, tenant_id).list_for_document(document_id)
            if document is not None
            else []
        )

    owns_store = store is None
    active = store or OpenSearchStore.from_settings(settings)
    try:
        await active.ensure_index()
        if expected_attempt is None:
            if document is None:
                # No replacement can race a truly absent source row; deleting
                # every derived generation is safe and repairs orphaned docs.
                await active.delete_document(
                    tenant_id=tenant_id, document_id=document_id, refresh=refresh
                )
                return IndexSyncResult(document_id, 0, deleted=True)

            snapshot_attempt = document.ingestion_attempts
            # Generic repair/reindex is allowed to publish only a completed,
            # current-space generation. Failed/processing rows and legacy-space
            # chunks remain absent from the derived store until ingestion makes
            # them Ready under this fingerprint (R1-002/R1-006).
            if (
                document.status is not DocumentStatus.READY
                or not chunks
                or any(
                    chunk.embedding_fingerprint != settings.embedding_space_fingerprint
                    for chunk in chunks
                )
            ):
                # The snapshot may already be stale. Exact + older cleanup can
                # never delete a replacement generation that claimed after the
                # read; a delete-all query can (R1-002).
                await active.delete_document_generation(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    ingestion_attempt=snapshot_attempt,
                    refresh=refresh,
                )
                await active.delete_older_document_generations(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    ingestion_attempt=snapshot_attempt,
                    refresh=refresh,
                )
                return IndexSyncResult(document_id, 0, deleted=True)

            # Repair orphaned ids from this exact generation without touching
            # any concurrently published replacement generation.
            await active.delete_document_generation(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=snapshot_attempt,
                refresh=refresh,
            )
        elif not chunks:
            await active.delete_document_generation(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=expected_attempt,
                refresh=refresh,
            )
            await active.delete_older_document_generations(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=expected_attempt,
                refresh=refresh,
            )
            return IndexSyncResult(document_id, 0, deleted=True)
        assert document is not None
        indexed = _to_indexed(
            document,
            chunks,
            embedding_fingerprint=settings.embedding_space_fingerprint,
        )
        try:
            await active.upsert_chunks(indexed, refresh=refresh)
        except DependencyError:
            # A bulk request may have committed a prefix before reporting a
            # partial failure. Delete only this attempt; never touch a newer
            # worker's generation (R1-001/R1-002).
            try:
                await active.delete_document_generation(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    ingestion_attempt=document.ingestion_attempts,
                    refresh=refresh,
                )
            except DependencyError:
                log.warning(
                    "index_sync.generation_cleanup_deferred",
                    tenant_id=str(tenant_id),
                    document_id=str(document_id),
                    attempt=document.ingestion_attempts,
                )
            raise
        if expected_attempt is not None:
            await active.delete_older_document_generations(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=expected_attempt,
                refresh=refresh,
            )
        else:
            await active.delete_older_document_generations(
                tenant_id=tenant_id,
                document_id=document_id,
                ingestion_attempt=snapshot_attempt,
                refresh=refresh,
            )
            # Publication used a DB snapshot taken before ensure/delete/upsert.
            # If a live worker claimed in that window, compensate by removing
            # only this stale snapshot. Hydration is fail-closed throughout;
            # the cleanup prevents the stale derived rows from lingering.
            async with tenant_session_scope(tenant_id) as session:
                current = await DocumentRepository(session, tenant_id).get(document_id)
            if (
                current is None
                or current.status is not DocumentStatus.READY
                or current.ingestion_attempts != snapshot_attempt
            ):
                await active.delete_document_generation(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    ingestion_attempt=snapshot_attempt,
                    refresh=refresh,
                )
                return IndexSyncResult(
                    document_id,
                    0,
                    deleted=True,
                    superseded=True,
                )
        return IndexSyncResult(document_id, len(chunks), deleted=False)
    finally:
        if owns_store:
            await active.aclose()


async def stamp_index_acl_stale_async(
    tenant_id: UUID,
    document_ids: list[UUID],
    *,
    settings: Settings,
    store: OpenSearchStore | None = None,
) -> None:
    """Propagate a framework ACL stale-stamp to the search index (ADR-0019 §3).

    Update-by-query on the stamped documents' chunk docs (``acl_synced_at →
    null``) so the engine-side freshness range denies them in the same run —
    the Postgres hydration re-check remains the enforcing backstop if this
    write fails. **Best-effort like every derived-index write**: a
    :class:`DependencyError` is the caller's to log-and-continue (the stamp is
    already durable in Postgres, and the idempotent reindex repairs the gap).
    """
    if not document_ids:
        return
    owns_store = store is None
    active = store or OpenSearchStore.from_settings(settings)
    try:
        await active.stamp_acl_stale(tenant_id=tenant_id, document_ids=document_ids)
    finally:
        if owns_store:
            await active.aclose()


async def attest_index_acl_fresh_async(
    tenant_id: UUID,
    document_ids: list[UUID],
    *,
    synced_at: datetime,
    settings: Settings,
    store: OpenSearchStore | None = None,
) -> None:
    """Propagate an unchanged-mirror attestation to the index (ADR-0019 §2/§3).

    The counterpart of :func:`stamp_index_acl_stale_async`: a complete,
    gap-free replay re-attests the mirrors it did not touch, and the engine's
    freshness range needs the new stamp or those documents would be missing
    from engine candidates until the next reindex. Issued in bounded batches so
    a large corpus can never turn into one unbounded update-by-query.

    Raises:
        DependencyError: the engine is unreachable/rejected the write. Purely a
            recall concern — Postgres is authoritative and its hydration
            re-check re-evaluates freshness — so the caller logs and continues.
    """
    if not document_ids:
        return
    owns_store = store is None
    active = store or OpenSearchStore.from_settings(settings)
    try:
        for start in range(0, len(document_ids), _ATTEST_BATCH_SIZE):
            await active.attest_acl_fresh(
                tenant_id=tenant_id,
                document_ids=document_ids[start : start + _ATTEST_BATCH_SIZE],
                synced_at=synced_at,
            )
    finally:
        if owns_store:
            await active.aclose()


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.sync_document_index",
    bind=True,
    acks_late=True,
    max_retries=None,  # the effective cap comes from settings.ingestion_max_retries
)
def sync_document_index(self: object, tenant_id: str, document_id: str) -> dict[str, object]:
    """Celery entrypoint: sync one document's search index (the sync wrapper).

    Mirrors the ingestion wrapper's retry semantics (same config knobs — the
    engine being down is the same class of transient fault): on
    :class:`DependencyError` retry with exponential backoff up to
    ``ingestion_max_retries``; when exhausted, **log and acknowledge** — the
    index is derived state, Postgres already committed, and the backfill
    (``python -m app.search.reindex``) repairs any gap. Never crash-loops the
    queue over a dead engine.
    """
    settings = get_settings()
    tid = UUID(tenant_id)
    did = UUID(document_id)
    try:
        result = run_task(sync_document_index_async(tid, did, settings=settings))
    except DependencyError as exc:
        request = getattr(self, "request", None)
        retries: int = getattr(request, "retries", 0) or 0
        if retries >= settings.ingestion_max_retries:
            log.warning(
                "index_sync.exhausted",
                tenant_id=tenant_id,
                document_id=document_id,
                error=exc.code,
                hint="index is stale for this document; run `python -m app.search.reindex`",
            )
            return {
                "document_id": document_id,
                "indexed_count": 0,
                "deleted": False,
                "error": exc.code,
            }
        countdown = settings.ingestion_retry_backoff_seconds * (2**retries)
        raise self.retry(exc=exc, countdown=countdown) from exc  # type: ignore[attr-defined]
    return {
        "document_id": str(result.document_id),
        "indexed_count": result.indexed_count,
        "deleted": result.deleted,
        "error": None,
    }


def enqueue_index_sync(tenant_id: UUID, document_id: UUID) -> None:
    """Enqueue an index sync for one document (the seam services call).

    The single enqueue point for ``lumen.sync_document_index`` (ADR-0004: only
    ``tasks/`` enqueues). Deletion paths call this **after commit** so the
    worker reads the already-deleted state and clears the index. Best-effort and
    bounded against the broker, exactly like :func:`app.tasks.ingest.enqueue_ingestion`:
    a broker outage is logged and swallowed (the index is derived and repairable
    by the backfill), never a 500 on a successful mutation.
    """
    from kombu.exceptions import OperationalError

    try:
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
            sync_document_index.apply_async(
                args=(str(tenant_id), str(document_id)),
                connection=connection,
                retry=False,
            )
    except OperationalError as exc:
        log.warning(
            "index_sync.enqueue_failed",
            document_id=str(document_id),
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )
