"""Trusted operator provisioning with explicit admin approval and atomic audit."""

from __future__ import annotations

from app.db.ocr import OcrRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.domain.ocr import OcrPolicy
from app.services.audit import AuditSink


async def configure_ocr_policy(
    repository: OcrRepository, audit: AuditSink, policy: OcrPolicy
) -> None:
    """Caller owns a tenant-bound transaction; repo verifies the approving admin."""
    await repository.configure(policy)
    await audit.emit(
        action=AuditAction.TENANT_SETTINGS_UPDATED,
        actor=AuditActor.user(policy.approved_by) if policy.approved_by else AuditActor.system(),
        resource_type="tenant",
        resource_id=str(policy.tenant_id),
        outcome=AuditOutcome.ALLOWED,
        request_id="ocr-policy-provisioning",
        source_ip="system",
        metadata={
            "setting": "hosted_ocr",
            "enabled": policy.enabled,
            "page_limit": policy.page_limit,
            "budget_usd": str(policy.budget_usd),
            "concurrency": policy.concurrency,
        },
    )
