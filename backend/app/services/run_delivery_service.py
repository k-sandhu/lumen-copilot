"""Scheduled-run delivery use-cases — the in-app inbox + digest (issue #238, ADR-0015 §6).

The orchestration layer for scheduled-run **delivery**: when a run reaches a
terminal, its output must land somewhere the owner sees it without opening the app.
v1 is **in-app only** (the T0/T1-safe default): an in-app **inbox** delivery on
completion, plus an opt-in **digest** that batches low-urgency runs into one
periodic in-app notification. External channels (email/Slack) are **T2-ish egress
and OUT of scope** here (INV-7) — the ``delivery`` jsonb leaves the seam, but no
external send happens; a future channel would be a new ``connectors/`` module **and**
a new AGENTS.md §6 boundary row in the same change.

Three concerns live here:

* **Produce** (:func:`deliver_run`) — the hook the run task calls on completion
  (:mod:`app.services.runs_service`): read the run's schedule delivery config (the
  ``delivery`` jsonb — ``inbox`` / ``digest``), and create a ``run_deliveries`` row.
  An ``inbox`` delivery is ``delivered`` immediately (visible now); a ``digest``
  schedule instead creates a ``pending`` digest delivery the beat batches later.
  Idempotent — a redelivered run task never double-notifies (INV-8 illegal repeat).
  A failure to produce is written as a ``failed`` delivery row, never a silent drop
  (AC-3).
* **Digest** (:func:`build_digest_for_tenant`) — the periodic beat sweep: roll a
  tenant's ``pending`` digest deliveries into ``delivered`` so they surface in the
  inbox as one batch (a low-urgency run does not ping the owner per fire).
* **Read** (:class:`RunDeliveryService`) — the ``GET /run-deliveries`` inbox (list,
  paginated) + mark-read, recipient- and tenant-scoped (a cross-tenant / non-owned
  delivery id → 404, existence non-disclosure, INV-1/INV-2), audited (INV-6).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).** Every
read is scoped to the caller's tenant (the repository) *and* the recipient
(``recipient_id``). The ``tenant_id``/``recipient_id`` come from the resolved
principal or the run's owner, never request input.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.repositories import (
    RunDeliveryRepository,
    RunRepository,
    ScheduleRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AuditOutcome,
    Run,
    RunDelivery,
    RunDeliveryKind,
    RunDeliveryStatus,
    ScheduleDelivery,
)
from app.services.audit import AuditSink, PermissionDeniedContext

log = get_logger(__name__)

_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

_DELIVERY_CURSOR_PREFIX = "del:"

# Bounded batch so a single digest sweep never scans unboundedly — the beat re-runs
# to drain a backlog (mirrors the artifact-retention janitor's batch limit).
_DIGEST_BATCH_LIMIT = 500


# --- Cursor codec (opaque; carries the boundary row id) ---------------------


def _encode_cursor(row_id: UUID) -> str:
    raw = f"{_DELIVERY_CURSOR_PREFIX}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_DELIVERY_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_DELIVERY_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


@dataclass(frozen=True, slots=True)
class RunDeliveryPage:
    """One page of run deliveries (newest first) + the opaque cursor for the next page."""

    items: list[RunDelivery]
    next_cursor: str | None


# --- Produce (the hook the run task calls on completion) --------------------


async def deliver_run(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    run: Run,
    audit: AuditSink,
    request_id: str = "run-task",
    source_ip: str = "system",
) -> RunDelivery | None:
    """Produce an in-app delivery for a completed run (ADR-0015 §6 — the run inbox).

    Called by the run task after the run reaches a terminal (from within its own
    transaction, so the delivery commits atomically with the run). Reads the run's
    schedule delivery config (the ``delivery`` jsonb) to decide the destination:

    * ``inbox`` (the v1 default) → a ``delivered`` :class:`RunDeliveryKind.INBOX`
      row the owner sees now, with the run's ``summary`` + a link to the transcript;
    * ``digest`` → a ``pending`` :class:`RunDeliveryKind.DIGEST` row the periodic
      digest beat batches into one notification (a low-urgency run does not ping the
      owner per fire). ``inbox`` and ``digest`` are **not exclusive**: a schedule may
      surface immediately *and* roll into the digest.

    A **manual** run (no schedule) always delivers to the inbox (there is no digest
    config to consult). Idempotent: a redelivered run task (Celery at-least-once)
    that already produced this run's inbox delivery is a no-op — a run never
    double-notifies. The recipient is the run **owner** (INV-2 — a delivery is
    addressed to whoever the run ran as), never request input.

    Returns the created (or existing) inbox delivery, or ``None`` when a digest-only
    schedule produced only a pending digest row. **A failure to produce is caught and
    written as a ``failed`` delivery row (AC-3), never a silent drop** — the run's own
    terminal is untouched.
    """
    deliveries = RunDeliveryRepository(session, tenant_id)
    config = await _delivery_config(session, tenant_id, run)

    inbox_delivery: RunDelivery | None = None
    try:
        # Digest-batched, low-urgency: a pending digest row the beat rolls up later.
        if config.digest is not None:
            if not await deliveries.exists_for_run(run.id, kind=RunDeliveryKind.DIGEST):
                await deliveries.create(
                    recipient_id=run.owner_id,
                    run_id=run.id,
                    schedule_id=run.schedule_id,
                    kind=RunDeliveryKind.DIGEST,
                    status=RunDeliveryStatus.PENDING,
                    summary=run.summary,
                )
        # Immediate inbox delivery (the default; also whenever inbox is opted in).
        if config.inbox and not await deliveries.exists_for_run(run.id, kind=RunDeliveryKind.INBOX):
            inbox_delivery = await deliveries.create(
                recipient_id=run.owner_id,
                run_id=run.id,
                schedule_id=run.schedule_id,
                kind=RunDeliveryKind.INBOX,
                status=RunDeliveryStatus.DELIVERED,
                summary=run.summary,
            )
    except Exception as exc:  # noqa: BLE001 — a delivery is never a silent drop (AC-3)
        log.error("run_delivery.produce_failed", run_id=str(run.id), error_type=type(exc).__name__)
        failed = await deliveries.create(
            recipient_id=run.owner_id,
            run_id=run.id,
            schedule_id=run.schedule_id,
            kind=RunDeliveryKind.INBOX,
            status=RunDeliveryStatus.FAILED,
            summary=run.summary,
        )
        await _audit_delivery(
            audit,
            action=AuditAction.RUN_DELIVERED,
            delivery=failed,
            run=run,
            request_id=request_id,
            source_ip=source_ip,
        )
        return failed

    delivered = inbox_delivery
    if delivered is not None:
        await _audit_delivery(
            audit,
            action=AuditAction.RUN_DELIVERED,
            delivery=delivered,
            run=run,
            request_id=request_id,
            source_ip=source_ip,
        )
    return delivered


async def _delivery_config(session: AsyncSession, tenant_id: UUID, run: Run) -> ScheduleDelivery:
    """The run's delivery config — its schedule's ``delivery`` jsonb, else the inbox default.

    A scheduled run reads its schedule's stored ``delivery`` (``inbox``/``digest``); a
    manual run (or a run whose schedule was deleted) falls back to the v1 default
    (land in the inbox, no digest) so a completed run *always* reaches the owner.
    """
    if run.schedule_id is not None:
        schedule = await ScheduleRepository(session, tenant_id).get(run.schedule_id)
        if schedule is not None:
            return schedule.delivery
    return ScheduleDelivery.default()


# --- Digest (the periodic beat sweep) ---------------------------------------


async def build_digest_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    audit: AuditSink | None = None,
    request_id: str = "digest-beat",
    source_ip: str = "system",
    batch_limit: int = _DIGEST_BATCH_LIMIT,
) -> int:
    """Roll a tenant's ``pending`` digest deliveries into ``delivered`` (ADR-0015 §6).

    The periodic digest beat drives this per tenant (under ``tenant_session_scope``):
    it sweeps up to ``batch_limit`` ``pending`` digest deliveries and marks each
    ``delivered`` so they surface in the owner's inbox as one batch — a low-urgency
    run is thereby notified once per digest window, not per fire. Idempotent: a re-run
    over an already-delivered set does nothing. Emits one ``run.digest_sent`` audit
    event per recipient batched (INV-6). Returns the number of deliveries batched.
    """
    deliveries = RunDeliveryRepository(session, tenant_id)
    pending = await deliveries.list_pending_digest(limit=batch_limit)
    per_recipient: dict[UUID, int] = {}
    for delivery in pending:
        await deliveries.mark_delivered(delivery.id)
        per_recipient[delivery.recipient_id] = per_recipient.get(delivery.recipient_id, 0) + 1
    if audit is not None:
        for recipient_id, count in per_recipient.items():
            await audit.emit(
                action=AuditAction.RUN_DIGEST_SENT,
                actor=AuditActor.user(recipient_id),
                resource_type="run_delivery",
                resource_id=str(recipient_id),
                outcome=AuditOutcome.ALLOWED,
                request_id=request_id,
                source_ip=source_ip,
                metadata={"batched": count},
            )
    return len(pending)


# --- Audit ------------------------------------------------------------------


async def _audit_delivery(
    audit: AuditSink,
    *,
    action: AuditAction,
    delivery: RunDelivery,
    run: Run | None,
    request_id: str,
    source_ip: str,
) -> None:
    """Emit a delivery audit event (INV-6) — actor is the delivery recipient.

    A delivery reaches the owner on their own behalf, so the audited actor is the
    recipient; the metadata tags the run + status so the trail shows what reached whom
    and how (delivered/failed/read).
    """
    metadata: dict[str, object] = {
        "run_id": str(delivery.run_id),
        "kind": delivery.kind.value,
        "status": delivery.status.value,
    }
    if run is not None and run.schedule_id is not None:
        metadata["schedule_id"] = str(run.schedule_id)
    await audit.emit(
        action=action,
        actor=AuditActor.user(delivery.recipient_id),
        resource_type="run_delivery",
        resource_id=str(delivery.id),
        outcome=AuditOutcome.ALLOWED,
        request_id=request_id,
        source_ip=source_ip,
        metadata=metadata,
    )


# --- Read service (GET /run-deliveries list + mark-read) --------------------


class RunDeliveryService:
    """List the caller's run deliveries + mark one read, recipient- and tenant-scoped (#238).

    Constructed per-request with the session + the resolved ``tenant_id`` /
    ``recipient_id`` (from the token, never request input) + the audit sink and
    correlation context. The repository enforces tenancy (INV-1); this service
    enforces the recipient rule — a delivery in another tenant or addressed to another
    user is **404** (existence non-disclosure), never 403.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        recipient_id: UUID,
        audit: AuditSink,
        denials: PermissionDeniedContext,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._deliveries = RunDeliveryRepository(session, tenant_id)
        self._runs = RunRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._recipient_id = recipient_id
        self._audit = audit
        self._denials = denials
        self._denials.assert_user(recipient_id)
        self._request_id = request_id
        self._source_ip = source_ip

    async def list_(
        self,
        *,
        cursor: str | None,
        limit: int | None,
        status: RunDeliveryStatus | None = None,
        unread_only: bool = False,
    ) -> RunDeliveryPage:
        """A keyset page of the caller's own deliveries (newest first), filterable."""
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._deliveries.list_for_recipient_page(
            self._recipient_id,
            limit=page_size + 1,
            after_id=after_id,
            status=status,
            unread_only=unread_only,
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return RunDeliveryPage(items=page, next_cursor=next_cursor)

    async def mark_read(self, delivery_id: UUID) -> RunDelivery:
        """Mark one delivery read; not visible/owned → 404 (INV-1/INV-2). Idempotent.

        Recipient-scoped (deny-by-default): a delivery in another tenant / addressed to
        another user is a 404 so the surface never reveals a foreign delivery. Audits
        ``run.delivery_read`` (INV-6). Re-marking an already-read delivery returns it
        unchanged.
        """
        existing = await self._deliveries.get(delivery_id)
        if existing is None or existing.recipient_id != self._recipient_id:
            await self._denials.emit(
                resource_type="run_delivery",
                resource_id=str(delivery_id),
                attempted_action="run.delivery.read",
                reason="not_visible",
            )
            raise NotFoundError("Delivery not found.")
        updated = await self._deliveries.mark_read(delivery_id, read_at=datetime.now(UTC))
        if updated is None:  # pragma: no cover — visibility already established
            raise NotFoundError("Delivery not found.")
        run = await self._runs.get(updated.run_id)
        await _audit_delivery(
            self._audit,
            action=AuditAction.RUN_DELIVERY_READ,
            delivery=updated,
            run=run,
            request_id=self._request_id,
            source_ip=self._source_ip,
        )
        return updated


__all__ = [
    "RunDeliveryPage",
    "RunDeliveryService",
    "build_digest_for_tenant",
    "deliver_run",
]
