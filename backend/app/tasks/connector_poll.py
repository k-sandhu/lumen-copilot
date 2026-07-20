"""Connector sync-poll beat task — periodic incremental syncs (ADR-0019 §3, #453).

The polling half of the change-detection cadence: on-demand syncs exist
(``POST /sources/{id}/sync``), and this **periodic beat task** keeps every
connected managed source fresh without user action — the static
``beat_schedule`` entry (registered in :func:`app.tasks.scheduler.configure_beat`)
fires ``lumen.poll_connector_syncs`` every ``CONNECTOR_SYNC_INTERVAL_MINUTES``.

Cross-tenant discovery runs under a **bypass**-scoped session (the one source
read that spans tenants, mirroring the run-digest sweep); each source is then
enqueued through the existing single enqueue seam
(:func:`app.tasks.sync_source.enqueue_source_sync`) so the **per-tenant fetch
rate limit** (ADR-0009 §3) applies unchanged — the poll can never make the
worker fan out unbounded fetches. Webhooks/push notification stay out (E7-2,
the epic scope fence).

The same beat carries the **stranded-ingestion sweep**. The incremental sync
commits a page's rows + cursor first and drives ingestion post-commit
(ADR-0019 §3's atomicity requirement is about the mutation/cursor pair); a
worker that dies in that window leaves a ``pending`` connector document with no
chunks, which the advanced cursor will never revisit and the reindex backfill
cannot repair — it can re-index chunks, not create them. The sweep re-drives
the **idempotent** ingestion task for connector documents left
``pending``/``processing`` past ``CONNECTOR_INGEST_RECOVERY_MINUTES``, which
needs no new outbox table and no new document state: converging a stuck row is
exactly what the existing status machine plus an idempotent task are for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from app.core.config import Settings, get_settings
from app.db.repositories import SourceReconcileRepository
from app.db.session import session_scope
from app.db.tenant_context import bind_bypass
from app.tasks.celery_app import celery_app
from app.tasks.ingest import enqueue_ingestion
from app.tasks.runner import run_task
from app.tasks.sync_source import enqueue_source_sync

log = structlog.get_logger(__name__)


async def _poll_all(settings: Settings) -> int:
    """Enqueue a sync for every pollable connected managed source.

    Pollable = credentialed (``auth_secret_ref``) and resting
    (``ready``/``error``) — an in-flight source is skipped by construction.
    Returns the number of syncs enqueued. Idempotent: a missed poll simply
    means the next interval catches up (a stalled sync also progressively
    hides ACL-mirrored content via the freshness window — fail closed).
    """
    del settings  # reserved for future per-poll knobs; discovery needs none
    async with session_scope() as session:
        await bind_bypass(session)
        pairs = await SourceReconcileRepository(session).list_connected_pollable()

    for tenant_id, source_id in pairs:
        # The single enqueue seam: per-tenant rate limit + bounded broker I/O.
        enqueue_source_sync(tenant_id, source_id)
    if pairs:
        log.info("connector_poll.enqueued", count=len(pairs))
    return len(pairs)


async def _sweep_stranded(settings: Settings) -> int:
    """Re-drive ingestion for connector documents stranded pre-ingestion.

    The recovery for the one window the per-page commit discipline cannot make
    atomic (ADR-0019 §3): the row + cursor commit, then ingestion runs
    post-commit. A crash in between leaves a ``pending`` document with no
    chunks — retrievable by nobody, and never revisited because the cursor
    already moved past its change.

    Finds those rows cross-tenant (bypass-scoped, bounded, and only past the
    ``CONNECTOR_INGEST_RECOVERY_MINUTES`` age threshold so a genuinely in-flight
    ingestion is left alone) and enqueues the **idempotent** ingestion task for
    each through its single enqueue seam. Re-driving a document that turned out
    to be healthy is harmless: the pipeline replaces its chunks.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=settings.connector_ingest_recovery_minutes)
    async with session_scope() as session:
        await bind_bypass(session)
        stranded = await SourceReconcileRepository(session).list_stranded_connector_documents(
            older_than=cutoff, limit=settings.connector_ingest_recovery_batch
        )

    for tenant_id, document_id in stranded:
        enqueue_ingestion(tenant_id, document_id)
    if stranded:
        log.warning(
            "connector_poll.stranded_documents_redriven",
            count=len(stranded),
            older_than_minutes=settings.connector_ingest_recovery_minutes,
        )
    return len(stranded)


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.poll_connector_syncs",
    acks_late=True,
)
def poll_connector_syncs() -> dict[str, object]:
    """Celery entrypoint the connector-sync beat fires (the sync wrapper).

    Drives :func:`_poll_all` and the stranded-ingestion recovery sweep
    (:func:`_sweep_stranded`) on a fresh event loop via ``run_task`` (engine
    disposed per run, #140). Returns a JSON-able summary.
    """
    settings = get_settings()

    async def _run() -> tuple[int, int]:
        return await _poll_all(settings), await _sweep_stranded(settings)

    enqueued, recovered = run_task(_run())
    return {"enqueued": enqueued, "recovered": recovered}
