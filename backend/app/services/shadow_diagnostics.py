"""Atomic diagnostic + audit storage through Python-owned boundaries."""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from app.db.ingestion_shadow import ShadowRepository
from app.db.ingestion_stages import StageRepository
from app.db.repositories import AuditEventRepository
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.domain.ingestion_shadow import ShadowComparison
from app.services.audit import AuditSink


async def record_comparison(
    tenant_id: UUID,
    document_id: UUID,
    *,
    fingerprint: str,
    comparison: ShadowComparison,
    attempt: int | None = None,
    actor: AuditActor | None = None,
) -> None:
    async with tenant_session_scope(tenant_id) as session:
        if attempt is not None:
            await StageRepository(session, tenant_id).ensure_owned(document_id, attempt=attempt)
        await ShadowRepository(session, tenant_id).record(document_id, fingerprint, comparison)
        await AuditSink(AuditEventRepository(session, tenant_id)).emit(
            action=AuditAction.DOCUMENT_PROCESSING_STAGE_COMPLETED,
            actor=actor or AuditActor.system(),
            resource_type="document",
            resource_id=str(document_id),
            outcome=AuditOutcome.ALLOWED,
            request_id="ingestion-shadow",
            source_ip="system",
            metadata={"stage": "shadow_compare", "fingerprint": fingerprint, **asdict(comparison)},
        )
