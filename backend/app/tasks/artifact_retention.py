"""Artifact retention janitor — purge expired artifacts (issue #208 §6, stub).

Files agents/runs produce (#208) may carry a ``retention_expires_at`` boundary
(stamped from ``ARTIFACT_RETENTION_DAYS`` at creation; NULL ⇒ keep forever). This
module is the **janitor** that purges rows past that boundary — the storage
object first, then the row — off the request path (backend/AGENTS.md: slow/burst
work is a Celery task, never in-request), idempotently and in bounded batches.

**Scope note (#208 §6 — "retention *optional* v1; stub the task, wire in E-Sched
later").** The async purge core (:func:`purge_expired_artifacts_async`) is real
and tested, but the janitor is **not yet scheduled**: there is deliberately no
Celery-beat entry enqueuing it periodically. Periodic scheduling belongs to the
scheduler epic (E-Sched); until then an operator (or that epic) triggers
:func:`purge_expired_artifacts` per tenant. Wiring a beat schedule is a
follow-up, not part of #208.

Tenant-scoped throughout (INV-1): the sweep runs *as* one tenant
(``tenant_session_scope`` binds the RLS GUC), reads only that tenant's expired
rows via the tenant-scoped :class:`~app.db.repositories.ArtifactRepository`, and
deletes each object via the #22 ``ObjectStore.delete_artifact`` (tenant-prefix
checked inside the adapter). A purge is a **system** action (no human in the
loop); it emits no per-object audit event here (the janitor is machinery, not a
user action — the create/download/delete *user* actions are audited in the
service). If a durable purge audit trail is wanted, it is an additive follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repositories import ArtifactRepository
from app.db.session import tenant_session_scope
from app.storage import ObjectStore
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task

# Bounded batch so a single sweep never scans/deletes unboundedly — the janitor
# re-runs to drain a large backlog. Kept a module constant (not config) because
# it is an internal batching detail, not an operator-tunable policy.
_PURGE_BATCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """The outcome of one retention sweep for a tenant — what the task returns.

    ``purged`` is the number of artifacts whose object **and** row were removed;
    ``scanned`` is how many expired rows the batch examined (``purged`` unless a
    row vanished mid-sweep, i.e. a concurrent delete).
    """

    tenant_id: UUID
    scanned: int
    purged: int


async def purge_expired_artifacts_async(
    tenant_id: UUID,
    *,
    now: datetime | None = None,
    object_store: ObjectStore | None = None,
    settings: Settings | None = None,
    batch_limit: int = _PURGE_BATCH_LIMIT,
) -> PurgeResult:
    """Purge one tenant's artifacts whose retention window has elapsed (#208 §6).

    Lists up to ``batch_limit`` expired rows (``retention_expires_at < now``,
    NULL-retention rows never selected), then for each removes the storage object
    (idempotent — a missing key is a no-op on S3/MinIO) and the row, in that order
    so a row never outlives its bytes. Tenant-scoped (INV-1): the session binds the
    RLS GUC and the repository/object-store are scoped to ``tenant_id``.
    Idempotent: a re-run over an already-purged set removes nothing.

    ``now`` / ``object_store`` / ``settings`` are injectable for tests; they
    default to the process singletons.
    """
    settings = settings or get_settings()
    store = object_store or ObjectStore(settings)
    moment = now or datetime.now(UTC)
    log = structlog.get_logger(__name__)

    async with tenant_session_scope(tenant_id) as session:
        repo = ArtifactRepository(session, tenant_id)
        expired = await repo.list_expired(now=moment, limit=batch_limit)
        purged = 0
        for artifact in expired:
            # Object first (idempotent), then the row — a row never outlives bytes.
            await store.delete_artifact(str(tenant_id), artifact.storage_key)
            if await repo.delete(artifact.id):
                purged += 1
        if expired:
            log.info(
                "artifact_retention.purged",
                tenant_id=str(tenant_id),
                scanned=len(expired),
                purged=purged,
            )
        return PurgeResult(tenant_id=tenant_id, scanned=len(expired), purged=purged)


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.purge_expired_artifacts",
    acks_late=True,
)
def purge_expired_artifacts(tenant_id: str) -> dict[str, object]:
    """Celery entrypoint: sweep one tenant's expired artifacts (the stub janitor).

    Drives :func:`purge_expired_artifacts_async` on a private event loop
    (``run_task`` disposes the DB engine afterwards, #140). Returns a JSON-able
    summary. **Not yet scheduled** (see the module docstring): periodic invocation
    is deferred to E-Sched; this exists so the purge is a defined, testable seam
    the scheduler epic can enqueue without new code here.
    """
    result = run_task(purge_expired_artifacts_async(UUID(tenant_id)))
    return {
        "tenant_id": str(result.tenant_id),
        "scanned": result.scanned,
        "purged": result.purged,
    }
