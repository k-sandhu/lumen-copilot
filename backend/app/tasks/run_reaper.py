"""Stale-run reaper beat task — fail abandoned run claims/retry handoffs (#578).

A worker that dies after claiming a run leaves it ``running`` forever:
Celery redelivery (``acks_late``) sees a non-``queued`` run and no-ops, and
the stuck run blocks every later fire of its schedule through the overlap
skip. This **periodic beat task** is the backstop: it fails runs still
``running`` or in a claimed ``queued`` retry handoff longer than
``RUN_STALE_AFTER_SECONDS`` (default six hours — above
the conservative model retry/turn envelope plus tool headroom; see
``core.config``), exactly once.

**Reuses the Beat/RedBeat wiring** (ADR-0004: ``tasks/`` owns background
jobs; the digest sweep is the sibling shape): the static ``beat_schedule``
entry (registered in :func:`app.tasks.scheduler.configure_beat` and
``celery_app``) fires ``lumen.reap_stale_runs`` at
``RUN_REAPER_INTERVAL_SECONDS``.

Cross-tenant discovery under a **bypass**-scoped session (the one run read
that spans tenants, mirroring the digest/schedule reconcile sweeps) finds
stale candidates; each is then failed *as* its tenant
(``tenant_session_scope`` binds the RLS GUC, INV-1) through
:func:`app.services.runs_service.reap_stale_run`, whose atomic
``UPDATE`` re-checks the running/claimed-queued predicate and age —
a run that finished concurrently is never overwritten, and a racing reaper
cannot double-audit or double-deliver. Bounded per sweep; the beat re-runs
to drain a backlog.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from app.core.config import Settings, get_settings
from app.db.repositories import RunReconcileRepository
from app.db.session import session_scope, tenant_session_scope
from app.db.tenant_context import bind_bypass
from app.services.runs_service import reap_stale_run
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task

log = structlog.get_logger(__name__)

# Bounded batch so one sweep never scans unboundedly — the beat re-runs to
# drain a backlog (mirrors the digest sweep's batch limit).
_REAP_BATCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class StaleRunReaperResult:
    """One sweep's tally: stale candidates scanned vs actually reaped."""

    scanned: int
    reaped: int


async def reap_stale_runs_async(
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> StaleRunReaperResult:
    """Fail stale running claims and queued retry handoffs, exactly once (#578).

    ``now`` is injectable so tests are deterministic (no wall-clock reads):
    the cutoff is ``now - run_stale_after_seconds``. Discovery is the
    deliberate bypass-scoped system read; each candidate is then failed in
    its own tenant-bound transaction via the CAS in
    :func:`app.services.runs_service.reap_stale_run`. A candidate that
    finished since discovery (or was reaped by a racing sweep) loses the CAS
    and is counted scanned-but-not-reaped. One candidate's failure never
    stops the sweep — its transaction rolls back and it stays eligible for
    the next sweep.
    """
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(seconds=settings.run_stale_after_seconds)
    async with session_scope() as session:
        await bind_bypass(session)
        candidates = await RunReconcileRepository(session).list_stale_running(
            older_than=cutoff, limit=_REAP_BATCH_LIMIT
        )

    reaped = 0
    for candidate in candidates:
        try:
            async with tenant_session_scope(candidate.tenant_id) as session:
                did_reap = await reap_stale_run(
                    session,
                    tenant_id=candidate.tenant_id,
                    run_id=candidate.id,
                    older_than=cutoff,
                    finished_at=moment,
                )
            if did_reap:
                reaped += 1
        except Exception as exc:  # noqa: BLE001 — one candidate must not stop the sweep
            log.warning(
                "run_reaper.candidate_failed",
                run_id=str(candidate.id),
                tenant_id=str(candidate.tenant_id),
                error_type=type(exc).__name__,
            )
    if reaped:
        log.info("run_reaper.reaped", scanned=len(candidates), reaped=reaped)
    return StaleRunReaperResult(scanned=len(candidates), reaped=reaped)


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.reap_stale_runs",
    acks_late=True,
)
def reap_stale_runs() -> dict[str, object]:
    """Celery entrypoint the reaper beat fires: fail abandoned run claims.

    Drives :func:`reap_stale_runs_async` on a fresh event loop via
    ``run_task`` (engine disposed per run, #140). Returns a JSON-able
    summary. Idempotent — an already-reaped (or concurrently finished) run
    loses the CAS on every later sweep.
    """
    result = run_task(reap_stale_runs_async())
    return {"scanned": result.scanned, "reaped": result.reaped}
