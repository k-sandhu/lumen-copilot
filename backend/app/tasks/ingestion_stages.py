"""Durable task checkpoints; the worker keeps all I/O and audit in Python."""

from __future__ import annotations

from uuid import UUID

from app.db.ingestion_stages import StageRepository
from app.db.repositories import AuditEventRepository
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.domain.ingestion_stages import StageOutput
from app.services.audit import AuditSink


class DurableStageStore:
    def __init__(self, tenant_id: UUID, document_id: UUID, attempt: int) -> None:
        self._tenant = tenant_id
        self._document = document_id
        self._attempt = attempt

    async def ensure_owned(self) -> None:
        async with tenant_session_scope(self._tenant) as session:
            await StageRepository(session, self._tenant).ensure_owned(
                self._document, attempt=self._attempt
            )

    async def get(self, stage: str, fingerprint: str) -> StageOutput | None:
        async with tenant_session_scope(self._tenant) as session:
            repo = StageRepository(session, self._tenant)
            await repo.ensure_owned(self._document, attempt=self._attempt)
            return await repo.get(self._document, stage, fingerprint)

    async def save(self, output: StageOutput) -> None:
        async with tenant_session_scope(self._tenant) as session:
            await StageRepository(session, self._tenant).save(
                self._document, output, attempt=self._attempt
            )
            await AuditSink(AuditEventRepository(session, self._tenant)).emit(
                action=AuditAction.DOCUMENT_PROCESSING_STAGE_COMPLETED,
                actor=AuditActor.system(),
                resource_type="document",
                resource_id=str(self._document),
                outcome=AuditOutcome.ALLOWED,
                request_id="document-ingestion-task",
                source_ip="system",
                metadata={
                    "stage": output.stage,
                    "fingerprint": output.fingerprint,
                    "output_sha256": output.output_sha256,
                },
            )
