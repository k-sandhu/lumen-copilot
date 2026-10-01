"""Capture transactional audit records for a context-rejected tool turn."""

from __future__ import annotations

import copy
from dataclasses import replace

from app.db.repositories import AuditEventRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditEvent, AuditOutcome
from app.services.audit import AuditSink


class ContextAuditSink(AuditSink):
    """Keep bounded, already validated records; durable events stay independent."""

    def __init__(self, repository: AuditEventRepository) -> None:
        super().__init__(repository)
        self.events: list[AuditEvent] = []

    async def emit(
        self,
        *,
        action: AuditAction | str,
        actor: AuditActor,
        resource_type: str,
        outcome: AuditOutcome,
        resource_id: str,
        request_id: str,
        source_ip: str,
        metadata: dict[str, object] | None = None,
        durable: bool = False,
    ) -> AuditEvent:
        event = await super().emit(
            action=action,
            actor=actor,
            resource_type=resource_type,
            outcome=outcome,
            resource_id=resource_id,
            request_id=request_id,
            source_ip=source_ip,
            metadata=metadata,
            durable=durable,
        )
        if not durable:
            self.events.append(replace(event, metadata=copy.deepcopy(event.metadata)))
        return event
