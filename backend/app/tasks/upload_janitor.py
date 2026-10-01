"""Periodic, bounded cleanup of abandoned direct multipart sessions (#571)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import DependencyError, NotFoundError, ValidationError
from app.db.repositories import (
    AuditEventRepository,
    DocumentUploadReconcileRepository,
    DocumentUploadRepository,
)
from app.db.session import session_scope, tenant_session_scope
from app.db.tenant_context import bind_bypass
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, DocumentUpload, DocumentUploadState
from app.services.audit import AuditSink, PermissionDeniedContext, PermissionDeniedRecorder
from app.services.document_upload_service import (
    DocumentUploadService,
    UploadCompletionRejected,
)
from app.storage import ObjectStore
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class UploadJanitorResult:
    scanned: int
    expired: int
    recovered: int


def _as_utc(value: datetime) -> datetime:
    """Normalise SQLite's timezone-naive round trip for deterministic sweeps."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _recovery_service(
    *,
    session: AsyncSession,
    candidate: DocumentUpload,
    store: ObjectStore,
    settings: Settings,
) -> DocumentUploadService:
    """Build the system-actor service used at the irreversible S3 boundary."""
    from app.db.session import get_durable_audit_transactions

    tenant_id = candidate.tenant_id
    return DocumentUploadService(
        session,
        tenant_id=tenant_id,
        owner_id=candidate.owner_id,
        store=store,
        audit=AuditSink(AuditEventRepository(session, tenant_id)),
        denials=PermissionDeniedContext(
            PermissionDeniedRecorder(
                get_durable_audit_transactions(settings),
                tenant_id=tenant_id,
                request_session=session,
            ),
            actor=AuditActor.system(),
            request_id="upload-janitor",
            source_ip="system",
        ),
        request_id="upload-janitor",
        source_ip="system",
        allowed_content_types=settings.upload_allowed_content_types,
        max_document_bytes=settings.max_upload_bytes,
        max_media_bytes=settings.max_media_upload_bytes,
        part_size_bytes=settings.upload_part_size_bytes,
        max_parts=settings.upload_max_parts,
        sign_batch_size=settings.upload_sign_batch_size,
        session_ttl_seconds=settings.upload_session_ttl_seconds,
        presign_ttl_seconds=settings.s3_presign_ttl_seconds,
        embedding_space_fingerprint=settings.embedding_space_fingerprint,
        audit_actor=AuditActor.system(),
    )


async def sweep_expired_uploads_async(
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    object_store: ObjectStore | None = None,
) -> UploadJanitorResult:
    """Abort/delete and durably expire one global bounded batch.

    Discovery is the deliberate bypass-scoped system read. Each mutation then
    re-enters a tenant-bound transaction, locks/rechecks the row, performs the
    idempotent storage cleanup, and atomically records state + audit.
    """
    settings = settings or get_settings()
    store = object_store or ObjectStore(settings)
    moment = now or datetime.now(UTC)
    async with session_scope() as session:
        await bind_bypass(session)
        expired = await DocumentUploadReconcileRepository(session).list_expired(
            now=moment, limit=settings.upload_janitor_batch_size
        )

    count = 0
    recovered = 0
    for candidate in expired:
        try:
            async with tenant_session_scope(candidate.tenant_id) as session:
                repo = DocumentUploadRepository(session, candidate.tenant_id)
                current = await repo.get_for_owner(candidate.id, candidate.owner_id, lock=True)
                if (
                    current is None
                    or current.state
                    not in {
                        DocumentUploadState.INITIATED,
                        DocumentUploadState.COMPLETING,
                    }
                    or _as_utc(current.expires_at) > _as_utc(moment)
                ):
                    continue
                expiry_reason = "upload_session_expired"
                if current.state is DocumentUploadState.COMPLETING:
                    try:
                        # HEAD + document/audit creation + post-commit enqueue are
                        # the same locked recovery path used by request retries.
                        await _recovery_service(
                            session=session,
                            candidate=current,
                            store=store,
                            settings=settings,
                        ).recover_completing(current.id)
                    except NotFoundError:
                        pass
                    except ValidationError as exc:
                        if exc.code not in {
                            "incomplete_provider_parts",
                            "invalid_provider_part_layout",
                            "invalid_provider_part_etag",
                        }:
                            raise
                        # A signed part can change after the durable boundary.
                        # At expiry these permanent validation failures must
                        # clean up and terminalize this candidate, not stop the
                        # global sweep. Provider outages still roll back/retry.
                        expiry_reason = exc.code
                    except UploadCompletionRejected as exc:
                        # The service has already made FAILED + object cleanup part
                        # of this transaction. Swallow so the terminal state commits
                        # and one corrupt object cannot stop the bounded sweep.
                        await _recovery_service(
                            session=session,
                            candidate=current,
                            store=store,
                            settings=settings,
                        ).audit_rejection(
                            operation="complete",
                            resource_type="document_upload",
                            resource_id=current.id,
                            error=exc,
                        )
                        continue
                    else:
                        recovered += 1
                        continue
                await store.abort_multipart_upload(
                    tenant_id=str(candidate.tenant_id),
                    key=current.storage_key,
                    provider_upload_id=current.provider_upload_id,
                )
                await store.delete(str(candidate.tenant_id), current.storage_key)
                await repo.set_state(
                    current.id,
                    current.owner_id,
                    DocumentUploadState.EXPIRED,
                    error=expiry_reason,
                )
                await AuditSink(AuditEventRepository(session, candidate.tenant_id)).emit(
                    action=AuditAction.DOCUMENT_UPLOAD_EXPIRED,
                    actor=AuditActor.system(),
                    resource_type="document_upload",
                    resource_id=str(current.id),
                    outcome=AuditOutcome.ERROR,
                    request_id="upload-janitor",
                    source_ip="system",
                    metadata={
                        "document_id": str(current.document_id),
                        **(
                            {"reason_code": expiry_reason}
                            if expiry_reason != "upload_session_expired"
                            else {}
                        ),
                    },
                )
                count += 1
        except DependencyError as exc:
            # A provider outage for one multipart session must not prevent the
            # bounded sweep from servicing independent tenants/sessions. The
            # transaction rolls back, leaving this row eligible for the retry.
            log.warning(
                "upload_janitor_storage_unavailable",
                tenant_id=str(candidate.tenant_id),
                upload_id=str(candidate.id),
                error_code=exc.code,
            )
    return UploadJanitorResult(scanned=len(expired), expired=count, recovered=recovered)


@celery_app.task(name="lumen.sweep_expired_uploads", acks_late=True)  # type: ignore[misc]
def sweep_expired_uploads() -> dict[str, int]:
    result = run_task(sweep_expired_uploads_async())
    return {
        "scanned": result.scanned,
        "expired": result.expired,
        "recovered": result.recovered,
    }
