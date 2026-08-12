"""Explicit in-memory durable-audit collaborator for offline tests (#579).

It is not a SQL transaction substitute: it records canonical ``AuditSink``
events in a separate in-memory ledger, so a StaticPool request Session is never
silently reused.  Real connection/commit/RLS behavior is covered by the targeted
disposable-Postgres suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entities import AuditEvent, AuditOutcome
from app.services.audit import PermissionDeniedRecorder


class _RecordingRepository:
    def __init__(self, ledger: RecordingDurableAuditTransactions, tenant_id: UUID) -> None:
        self._ledger = ledger
        self.tenant_id = tenant_id

    async def record(
        self,
        *,
        action: str,
        resource_type: str,
        outcome: AuditOutcome,
        actor_id: UUID | None = None,
        resource_id: str | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AuditEvent:
        if source_ip == "system":
            origin, stored_ip = "system", None
        elif source_ip in (None, "", "unknown"):
            origin, stored_ip = "unknown", None
        else:
            origin, stored_ip = "client", source_ip.strip()
        event = AuditEvent(
            id=uuid4(),
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
        self._ledger.events.append(event)
        return event


class RecordingDurableAuditTransactions:
    """A separate, inspectable ledger implementing the provider surface."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.fail_with: BaseException | None = None

    def assert_independent_from(self, request_session: object) -> None:
        del request_session

    @asynccontextmanager
    async def repository(self, tenant_id: UUID) -> AsyncIterator[_RecordingRepository]:
        if self.fail_with is not None:
            raise self.fail_with
        yield _RecordingRepository(self, tenant_id)


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


__all__ = [
    "RecordingDurableAuditTransactions",
    "denial_recorder",
    "denial_recorder_from_session",
]
