"""Short tenant-bound OCR accounting/audit transactions in ingestion workers."""

from __future__ import annotations

from uuid import UUID

from app.db.ingestion_stages import StageRepository
from app.db.ocr import OcrRepository
from app.db.repositories import AuditEventRepository
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.domain.ocr import OcrPolicy, OcrResult
from app.services.audit import AuditSink


class DurableOcrLedger:
    def __init__(
        self, tenant_id: UUID, document_id: UUID, attempt: int, *, model: str | None = None
    ) -> None:
        self.tenant_id = tenant_id
        self._document = document_id
        self._attempt = attempt
        self._model = model

    async def policy(self) -> OcrPolicy:
        async with tenant_session_scope(self.tenant_id) as session:
            await StageRepository(session, self.tenant_id).ensure_owned(
                self._document, attempt=self._attempt
            )
            return await OcrRepository(session, self.tenant_id).policy()

    async def cached(self, digest: str, engine: str) -> OcrResult | None:
        async with tenant_session_scope(self.tenant_id) as session:
            await StageRepository(session, self.tenant_id).ensure_owned(
                self._document, attempt=self._attempt
            )
            return await OcrRepository(session, self.tenant_id).cached(digest, engine)

    async def begin(self, digest: str, engine: str, page: int, policy: OcrPolicy) -> None:
        async with tenant_session_scope(self.tenant_id) as session:
            await StageRepository(session, self.tenant_id).ensure_owned(
                self._document, attempt=self._attempt
            )
            await OcrRepository(session, self.tenant_id).begin(digest, engine, policy)
            await AuditSink(AuditEventRepository(session, self.tenant_id)).emit(
                action=AuditAction.DOCUMENT_OCR_REQUESTED,
                actor=AuditActor.system(),
                resource_type="document",
                resource_id=str(self._document),
                outcome=AuditOutcome.ALLOWED,
                request_id="document-ingestion-task",
                source_ip="system",
                metadata={
                    "page": page,
                    "engine": engine,
                    "model": self._model,
                    "content_sha256": digest,
                    "approved_by": str(policy.approved_by),
                    "ceiling_usd": str(policy.per_page_ceiling_usd),
                },
            )

    async def finish(
        self, digest: str, engine: str, result: OcrResult | None, error: str | None
    ) -> None:
        # Spend/cache settlement must survive ownership loss after dispatch; no generation writes.
        async with tenant_session_scope(self.tenant_id) as session:
            await OcrRepository(session, self.tenant_id).finish(digest, engine, result, error)
            await AuditSink(AuditEventRepository(session, self.tenant_id)).emit(
                action=AuditAction.DOCUMENT_OCR_COMPLETED,
                actor=AuditActor.system(),
                resource_type="document",
                resource_id=str(self._document),
                outcome=AuditOutcome.ERROR if error else AuditOutcome.ALLOWED,
                request_id="document-ingestion-task",
                source_ip="system",
                metadata={
                    "engine": engine,
                    "content_sha256": digest,
                    "error": error,
                    "cost_usd": str(result.cost_usd)
                    if result and result.cost_usd is not None
                    else None,
                    "input_tokens": result.input_tokens if result else None,
                    "output_tokens": result.output_tokens if result else None,
                    "reported_model": result.reported_model if result else None,
                },
            )
