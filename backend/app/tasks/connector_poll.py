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
"""

from __future__ import annotations

import structlog

from app.core.config import Settings, get_settings
from app.db.repositories import SourceReconcileRepository
from app.db.session import session_scope
from app.db.tenant_context import bind_bypass
from app.tasks.celery_app import celery_app
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


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.poll_connector_syncs",
    acks_late=True,
)
def poll_connector_syncs() -> dict[str, object]:
    """Celery entrypoint the connector-sync beat fires (the sync wrapper).

    Drives :func:`_poll_all` on a fresh event loop via ``run_task`` (engine
    disposed per run, #140). Returns a JSON-able summary.
    """
    settings = get_settings()
    enqueued = run_task(_poll_all(settings))
    return {"enqueued": enqueued}
