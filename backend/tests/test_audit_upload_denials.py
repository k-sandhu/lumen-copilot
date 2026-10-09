"""Merged media service seams retain #579's direct-call denial guarantees."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks  # noqa: F401 — load the task registry before its service entry point
from app.auth.principal import Principal
from app.core.errors import ForbiddenError, NotFoundError
from app.domain.audit import AuditActor
from app.domain.entities import DocumentUploadState
from app.services.audit import AuditSink, PermissionDeniedContext
from app.services.document_upload_service import DocumentAccessService, DocumentUploadService
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_recorder


def _service(*, system=False):
    session = AsyncSession()
    tenant_id, owner_id = uuid4(), uuid4()
    ledger = RecordingDurableAuditTransactions()
    denials = PermissionDeniedContext(
        denial_recorder(ledger, session, tenant_id),
        principal=None if system else Principal(owner_id, tenant_id, ()),
        actor=AuditActor.system() if system else None,
        request_id="merged-upload-denial",
        source_ip="system" if system else "203.0.113.60",
    )
    service = DocumentUploadService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        store=MagicMock(),
        audit=MagicMock(spec=AuditSink),
        denials=denials,
        request_id=denials.request_id,
        source_ip=denials.source_ip,
        allowed_content_types=frozenset({"text/plain"}),
        max_document_bytes=100,
        max_media_bytes=100,
        part_size_bytes=10,
        max_parts=10,
        sign_batch_size=10,
        session_ttl_seconds=100,
        presign_ttl_seconds=10,
        embedding_space_fingerprint="test",
        audit_actor=AuditActor.system() if system else None,
    )
    return service, ledger, session


@pytest.mark.parametrize(
    "method,action",
    [
        ("initiate", "document.upload"),
        ("get", "document_upload.read"),
        ("expire_if_needed", "document_upload.expire"),
        ("prepare_sign_parts", "document_upload.sign_parts"),
        ("prepare_abort", "document_upload.abort"),
        ("prepare_complete", "document_upload.complete"),
        ("sign_parts", "document_upload.sign_parts"),
        ("abort", "document_upload.abort"),
        ("complete", "document_upload.complete"),
        ("recover_completing", "document_upload.recover"),
    ],
)
async def test_direct_upload_missing_result_has_one_durable_denial(method, action):
    service, ledger, session = _service()
    target = uuid4()
    service._collections.get = AsyncMock(return_value=None)
    service._uploads.get_for_owner = AsyncMock(return_value=None)
    try:
        if method == "initiate":
            result = await service.initiate(
                collection_id=target,
                filename="private.txt",
                mime_type="text/plain",
                size_bytes=10,
                last_modified_at=None,
            )
        elif method == "sign_parts":
            result = await service.sign_parts(target, [1])
        elif method == "complete":
            result = await service.complete(target, [])
        else:
            result = await getattr(service, method)(target)
        assert result is None
        await session.rollback()
        assert len(ledger.events) == 1
        event = ledger.events[0]
        assert (event.actor_id, event.tenant_id) == (service._owner_id, service._tenant_id)
        assert event.resource_id == str(target)
        assert event.metadata == {"attempted_action": action, "reason": "not_visible"}
        assert (event.request_id, event.source_origin, event.source_ip) == (
            "merged-upload-denial",
            "client",
            "203.0.113.60",
        )
        service._audit.emit.assert_not_called()
    finally:
        await session.close()


async def test_completed_upload_missing_document_and_system_recovery_use_same_helper():
    service, ledger, session = _service(system=True)
    upload_id = uuid4()
    service._uploads.get_for_owner = AsyncMock(
        return_value=SimpleNamespace(state=DocumentUploadState.COMPLETED, document_id=uuid4())
    )
    service._documents.get = AsyncMock(return_value=None)
    try:
        assert await service.recover_completing(upload_id) is None
        assert await service.complete(upload_id, []) is None
        assert len(ledger.events) == 2
        assert all(
            event.actor_id is None and event.source_origin == "system" for event in ledger.events
        )
        assert {event.metadata["attempted_action"] for event in ledger.events} == {
            "document_upload.recover",
            "document_upload.complete",
        }
    finally:
        await session.close()


@pytest.mark.parametrize("method", ["create_access_url", "get_transcript"])
@pytest.mark.parametrize("error", [None, NotFoundError("missing"), ForbiddenError("denied")])
async def test_direct_media_terminal_denial_and_sink_failure_propagation(method, error):
    upload_service, ledger, session = _service()
    service = DocumentAccessService(
        session,
        tenant_id=upload_service._tenant_id,
        owner_id=upload_service._owner_id,
        store=MagicMock(),
        audit=MagicMock(spec=AuditSink),
        denials=upload_service._denials,
        request_id="merged-upload-denial",
        source_ip="203.0.113.60",
        presign_ttl_seconds=10,
    )
    service._visible = AsyncMock(return_value=None, side_effect=error)
    target = uuid4()

    async def attempt():
        if method == "create_access_url":
            return await service.create_access_url(target, purpose="preview")
        return await service.get_transcript(target, cursor=None, limit=10, around_ms=None)

    try:
        if error is None:
            assert await attempt() is None
        else:
            with pytest.raises(type(error)):
                await attempt()
        assert len(ledger.events) == 1
        assert ledger.events[0].resource_id == str(target)
        service._audit.emit.assert_not_called()
        ledger.fail_with = RuntimeError("audit unavailable")
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await attempt()
        assert len(ledger.events) == 1
    finally:
        await session.close()
