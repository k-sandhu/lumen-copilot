"""Explicit in-memory durable-audit collaborator for offline tests (#579).

It is not a SQL transaction substitute: it records canonical ``AuditSink``
events in a separate in-memory ledger, so a StaticPool request Session is never
silently reused.  Real connection/commit/RLS behavior is covered by the targeted
disposable-Postgres suite.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import _classify_source_ip
from app.domain.audit import AuditActor
from app.domain.entities import AuditEvent, AuditOutcome
from app.services.audit import PermissionDeniedContext, PermissionDeniedRecorder


class _RecordingRepository:
    def __init__(self, ledger: RecordingDurableAuditTransactions, tenant_id: UUID) -> None:
        self._ledger = ledger
        self.tenant_id = tenant_id

    async def record(
        self,
        *,
        event_id: UUID | None = None,
        action: str,
        resource_type: str,
        outcome: AuditOutcome,
        actor_id: UUID | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AuditEvent:
        classified_origin, stored_ip, _unrecognised = _classify_source_ip(source_ip)
        origin = classified_origin.value
        identity = event_id or uuid4()
        event = AuditEvent(
            id=identity,
            tenant_id=self.tenant_id,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            request_id=request_id,
            source_origin=origin,
            source_ip=stored_ip,
            metadata=metadata or {},
            ts=datetime.now(UTC),
        )
        existing = next((row for row in self._ledger.events if row.id == identity), None)
        if existing is not None:
            if (
                existing.tenant_id,
                existing.actor_id,
                existing.action,
                existing.resource_type,
                existing.resource_id,
                existing.outcome,
                existing.request_id,
                existing.source_origin,
                existing.source_ip,
                existing.metadata,
            ) != (
                event.tenant_id,
                event.actor_id,
                event.action,
                event.resource_type,
                event.resource_id,
                event.outcome,
                event.request_id,
                event.source_origin,
                event.source_ip,
                event.metadata,
            ):
                raise RuntimeError(
                    "Audit idempotency key resolved to a different canonical payload."
                )
            return existing
        self._ledger.events.append(event)
        return event

    async def get(self, event_id: UUID) -> AuditEvent | None:
        return next(
            (
                event
                for event in self._ledger.events
                if event.tenant_id == self.tenant_id and event.id == event_id
            ),
            None,
        )


class RecordingDurableAuditTransactions:
    """A separate, inspectable ledger implementing the provider surface."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.fail_with: BaseException | None = None

    def assert_independent_from(self, request_session: object) -> None:
        del request_session

    async def execute_idempotent(
        self,
        tenant_id: UUID,
        event_id: UUID,
        operation: Callable[[_RecordingRepository], Awaitable[AuditEvent]],
    ) -> AuditEvent:
        if self.fail_with is not None:
            raise self.fail_with
        event = await operation(_RecordingRepository(self, tenant_id))
        if event.id != event_id:
            raise RuntimeError("Durable audit operation returned a different idempotency key.")
        return event


def denial_recorder(
    ledger: RecordingDurableAuditTransactions,
    request_session: object,
    tenant_id: UUID,
) -> PermissionDeniedRecorder:
    """Build the production recorder over the explicit test ledger."""
    return PermissionDeniedRecorder(  # type: ignore[arg-type]
        ledger,
        tenant_id=tenant_id,
        request_session=request_session,
    )


def denial_recorder_from_session(
    request_session: AsyncSession,
    tenant_id: UUID,
) -> PermissionDeniedRecorder:
    """Read the explicitly injected test ledger from a Session fixture."""
    ledger = request_session.info.get("durable_audit_ledger")
    if not isinstance(ledger, RecordingDurableAuditTransactions):
        raise RuntimeError("Test Session is missing its durable-audit ledger.")
    return denial_recorder(ledger, request_session, tenant_id)


def denial_context(
    ledger: RecordingDurableAuditTransactions,
    request_session: object,
    tenant_id: UUID,
    actor_id: UUID,
    *,
    request_id: str = "req-test-denial",
    source_ip: str = "203.0.113.7",
) -> PermissionDeniedContext:
    """Build the mandatory production context over the explicit test ledger."""
    return PermissionDeniedContext(
        denial_recorder(ledger, request_session, tenant_id),
        actor=AuditActor.user(actor_id),
        request_id=request_id,
        source_ip=source_ip,
    )


__all__ = [
    "RecordingDurableAuditTransactions",
    "denial_context",
    "denial_recorder",
    "denial_recorder_from_session",
]
