"""Run-digest beat task — batch pending low-urgency deliveries into an in-app digest (#238).

The delivery *digest* half of scheduled-run delivery (ADR-0015 §6): a completed run
whose schedule opted into a digest lands as a ``pending`` ``run_deliveries`` row
(:mod:`app.services.run_delivery_service`), and this **periodic beat task** rolls a
window's pending deliveries into one in-app notification per recipient — so a
low-urgency run notifies the owner once per digest window, not per fire.

**Reuses the #236 celery-beat / RedBeat wiring** (ADR-0004: ``tasks/`` owns
background jobs; RedBeat rides the existing Redis broker — no new datastore). The
static ``beat_schedule`` entry (registered in
:func:`app.tasks.scheduler.configure_beat`) fires ``lumen.build_run_digests`` at
``RUN_DIGEST_INTERVAL_SECONDS``; this task fans the sweep out per tenant.

Cross-tenant discovery under a **bypass**-scoped session (the one delivery read that
spans tenants, mirroring the schedule reconcile sweep) finds which tenants have
pending digest deliveries; the per-tenant batch then runs *as* that tenant
(``tenant_session_scope`` binds the RLS GUC, INV-1). Idempotent: a re-run over an
already-delivered set does nothing. A run is never silently dropped — a pending
digest delivery is a durable row that surfaces on the next sweep even if one is
missed.
"""

from __future__ import annotations

import structlog

from app.core.config import Settings, get_settings
from app.db.repositories import AuditEventRepository, RunDeliveryReconcileRepository
from app.db.session import session_scope, tenant_session_scope
from app.db.tenant_context import bind_bypass
from app.services.audit import AuditSink
from app.services.run_delivery_service import build_digest_for_tenant
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task

log = structlog.get_logger(__name__)


async def _build_all_digests(settings: Settings) -> int:
    """Sweep every tenant with pending digest deliveries; batch each (ADR-0015 §6).

    Discovers the tenant set cross-tenant under a bypass session (system-only path,
    never a request), then batches each tenant's pending digest deliveries *as* that
    tenant. Returns the total number of deliveries batched across all tenants.
    """
    async with session_scope() as session:
        await bind_bypass(session)
        tenant_ids = await RunDeliveryReconcileRepository(session).tenants_with_pending_digest()

    total = 0
    for tenant_id in tenant_ids:
        async with tenant_session_scope(tenant_id) as session:
            audit = AuditSink(AuditEventRepository(session, tenant_id))
            total += await build_digest_for_tenant(
                session,
                tenant_id=tenant_id,
                audit=audit,
            )
    if total:
        log.info("run_digest.batched", tenants=len(tenant_ids), delivered=total)
    return total


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.build_run_digests",
    acks_late=True,
)
def build_run_digests() -> dict[str, object]:
    """Celery entrypoint the digest beat fires: batch pending digest deliveries.

    Drives :func:`_build_all_digests` on a fresh event loop via ``run_task`` (engine
    disposed per run, #140). Returns a JSON-able summary. Idempotent — a pending
    delivery not yet swept simply carries to the next window (never a silent drop).
    """
    settings = get_settings()
    delivered = run_task(_build_all_digests(settings))
    return {"delivered": delivered}
