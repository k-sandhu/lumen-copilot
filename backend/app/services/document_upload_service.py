"""Direct multipart upload and signed media-access use cases (spec 0008)."""

from __future__ import annotations

import base64
import binascii
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

import app.tasks as tasks
from app.core.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    DocumentUploadRepository,
    GroupRepository,
    TranscriptRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AuditOutcome,
    Document,
    DocumentKind,
    DocumentStatus,
    DocumentUpload,
    DocumentUploadState,
    TranscriptSegment,
    TranscriptSpeaker,
)
from app.retrieval.permissions import AllowSet
from app.retrieval.queries import get_permitted_document
from app.services.audit import AuditSink
from app.storage import ObjectStore, StoredObjectMetadata, UploadedPart
from app.storage.validation import (
    canonical_content_type_for_filename,
    document_kind_for_content_type,
    normalize_content_type,
    validate_upload,
)

UploadControlOperation = Literal["initiate", "get", "sign_parts", "abort", "complete"]

_REJECTION_ACTION_BY_OPERATION: dict[UploadControlOperation, AuditAction] = {
    "initiate": AuditAction.DOCUMENT_UPLOAD_STARTED,
    "get": AuditAction.DOCUMENT_VIEWED,
    "sign_parts": AuditAction.DOCUMENT_UPLOAD_STARTED,
    "abort": AuditAction.DOCUMENT_UPLOAD_ABORTED,
    "complete": AuditAction.DOCUMENT_UPLOADED,
}


@dataclass(frozen=True, slots=True)
class UploadPartView:
    part_number: int
    etag: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class UploadSessionView:
    upload: DocumentUpload
    completed_parts: tuple[UploadPartView, ...]
    document: Document | None = None


@dataclass(frozen=True, slots=True)
class SignedPartView:
    part_number: int
    url: str
    expires_at: datetime
    required_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class CompletePartInput:
    part_number: int
    etag: str


@dataclass(frozen=True, slots=True)
class AccessCapability:
    url: str
    document: Document
    purpose: str
    expires_at: datetime
    supports_byte_ranges: bool = True


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    document: Document
    speakers: tuple[TranscriptSpeaker, ...]
    items: tuple[TranscriptSegment, ...]
    next_cursor: str | None


class UploadCompletionRejected(AppError):
    """A terminal post-storage verification failure that must be committed."""


def _is_valid_etag(etag: str) -> bool:
    """Accept a bounded opaque ETag while excluding controls (OpenAPI contract)."""
    return 1 <= len(etag) <= 512 and all(ord(char) >= 0x20 and ord(char) != 0x7F for char in etag)


def _map_validation(error: ValidationError) -> AppError:
    if error.code in {"content_type_not_allowed", "filename_extension_not_allowed"}:
        return UnsupportedMediaTypeError(error.detail, code=error.code)
    if error.code == "upload_too_large":
        return PayloadTooLargeError(error.detail, code=error.code)
    return error


class DocumentUploadService:
    """Authenticated control plane; no method accepts file bytes."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        store: ObjectStore,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
        allowed_content_types: frozenset[str],
        max_document_bytes: int,
        max_media_bytes: int,
        part_size_bytes: int,
        max_parts: int,
        sign_batch_size: int,
        session_ttl_seconds: int,
        presign_ttl_seconds: int,
        audit_actor: AuditActor | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._store = store
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        self._allowed_content_types = allowed_content_types
        self._max_document_bytes = max_document_bytes
        self._max_media_bytes = max_media_bytes
        self._part_size_bytes = part_size_bytes
        self._max_parts = max_parts
        self._sign_batch_size = sign_batch_size
        self._session_ttl_seconds = session_ttl_seconds
        self._presign_ttl_seconds = presign_ttl_seconds
        self._audit_actor = audit_actor or AuditActor.user(owner_id)
        self._uploads = DocumentUploadRepository(session, tenant_id)
        self._documents = DocumentRepository(session, tenant_id)
        self._collections = CollectionRepository(session, tenant_id)

    async def audit_rejection(
        self,
        *,
        operation: UploadControlOperation,
        resource_type: str,
        resource_id: UUID,
        error: AppError,
        permission_denied: bool = False,
    ) -> None:
        """Emit content-safe evidence for one rejected control-plane attempt.

        The router owns the transaction boundary: it first rolls back any
        non-durable work (unless a terminal failure must be preserved), calls
        this method, and commits the audit-only transaction before re-raising.
        Missing, foreign-tenant, and non-owned resources deliberately share one
        reason code and 404 response so the audit trail cannot become an
        existence oracle. Provider ids, keys, URLs, filenames, and content are
        never metadata here.
        """
        await self._audit.emit(
            action=(
                AuditAction.PERMISSION_DENIED
                if permission_denied
                else _REJECTION_ACTION_BY_OPERATION[operation]
            ),
            actor=self._audit_actor,
            resource_type=resource_type,
            resource_id=str(resource_id),
            outcome=(AuditOutcome.DENIED if permission_denied else AuditOutcome.ERROR),
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "operation": operation,
                "reason_code": ("not_found_or_not_owned" if permission_denied else error.code),
                "status": error.status,
            },
        )

    async def initiate(
        self,
        *,
        collection_id: UUID,
        filename: str,
        mime_type: str,
        size_bytes: int,
        last_modified_at: datetime | None,
    ) -> UploadSessionView | None:
        # Serialize with collection deletion. The lock is held through provider
        # multipart creation and this request's row/audit commit, so DELETE can
        # never scan-before-init then cascade away the only cleanup handle.
        collection = await self._collections.get(collection_id, lock=True)
        if collection is None or collection.owner_id != self._owner_id:
            return None
        if len(filename) > 512:
            raise ValidationError("filename is too long", code="invalid_filename")
        if len(mime_type) > 255:
            raise ValidationError("mime_type is too long", code="invalid_mime_type")
        filename = filename.strip()
        if not filename:
            raise ValidationError("filename must not be blank", code="missing_filename")
        try:
            mime_type = canonical_content_type_for_filename(filename, mime_type)
        except ValidationError as exc:
            raise _map_validation(exc) from exc
        kind = document_kind_for_content_type(mime_type)
        cap = (
            self._max_media_bytes if kind is not DocumentKind.DOCUMENT else self._max_document_bytes
        )
        try:
            validate_upload(
                size_bytes=size_bytes,
                content_type=mime_type,
                allowed_content_types=self._allowed_content_types,
                max_bytes=cap,
            )
        except ValidationError as exc:
            raise _map_validation(exc) from exc
        part_count = math.ceil(size_bytes / self._part_size_bytes)
        if part_count > self._max_parts:
            raise PayloadTooLargeError(
                "The file requires more multipart parts than this deployment allows.",
                code="too_many_upload_parts",
            )

        upload_id = uuid4()
        document_id = uuid4()
        multipart = await self._store.create_multipart_upload(
            tenant_id=str(self._tenant_id),
            document_id=str(document_id),
            upload_id=str(upload_id),
            filename=filename,
            content_type=normalize_content_type(mime_type),
        )
        try:
            upload = await self._uploads.create(
                upload_id=upload_id,
                document_id=document_id,
                owner_id=self._owner_id,
                collection_id=collection_id,
                filename=filename,
                mime_type=normalize_content_type(mime_type),
                size_bytes=size_bytes,
                storage_key=multipart.key,
                provider_upload_id=multipart.provider_upload_id,
                part_size_bytes=self._part_size_bytes,
                part_count=part_count,
                last_modified_at=last_modified_at,
                expires_at=datetime.now(UTC) + timedelta(seconds=self._session_ttl_seconds),
            )
            await self._audit.emit(
                action=AuditAction.DOCUMENT_UPLOAD_STARTED,
                actor=self._audit_actor,
                resource_type="document_upload",
                resource_id=str(upload.id),
                outcome=AuditOutcome.ALLOWED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={
                    "document_id": str(document_id),
                    "collection_id": str(collection_id),
                    "filename": filename,
                    "mime_type": upload.mime_type,
                    "size_bytes": size_bytes,
                    "part_count": part_count,
                },
            )
        except Exception:
            await self._store.abort_multipart_upload(
                tenant_id=str(self._tenant_id),
                key=multipart.key,
                provider_upload_id=multipart.provider_upload_id,
            )
            raise
        return UploadSessionView(upload=upload, completed_parts=())

    async def get(self, upload_id: UUID) -> UploadSessionView | None:
        # GET may perform expiry or crash recovery, so it participates in the
        # same row-lock serialization as sign/complete/abort.
        upload = await self._uploads.get_for_owner(upload_id, self._owner_id, lock=True)
        if upload is None:
            return None
        if upload.state is DocumentUploadState.COMPLETING:
            document = await self._recover_completing_upload(upload)
            refreshed = await self._uploads.get_for_owner(upload.id, self._owner_id)
            assert refreshed is not None
            return UploadSessionView(upload=refreshed, completed_parts=(), document=document)
        upload = await self._expire_if_needed(upload)
        return await self._view(upload)

    async def expire_if_needed(self, upload_id: UUID) -> UploadSessionView | None:
        """Public terminal-expiry seam for routers to commit before returning 409."""
        upload = await self._uploads.get_for_owner(upload_id, self._owner_id, lock=True)
        if upload is None:
            return None
        if upload.state is DocumentUploadState.COMPLETING:
            document = await self._recover_completing_upload(upload)
            refreshed = await self._uploads.get_for_owner(upload.id, self._owner_id)
            assert refreshed is not None
            return UploadSessionView(upload=refreshed, completed_parts=(), document=document)
        terminal = await self._expire_if_needed(upload)
        return UploadSessionView(upload=terminal, completed_parts=())

    async def sign_parts(
        self, upload_id: UUID, part_numbers: list[int]
    ) -> list[SignedPartView] | None:
        upload = await self._uploads.get_for_owner(upload_id, self._owner_id, lock=True)
        if upload is None:
            return None
        if self._is_expired(upload):
            raise ConflictError("Upload session has expired.", code="upload_session_expired")
        if upload.state is not DocumentUploadState.INITIATED:
            raise ConflictError(
                "Upload does not accept parts in its current state.",
                code="upload_state_conflict",
            )
        if (
            not part_numbers
            or len(part_numbers) > self._sign_batch_size
            or len(set(part_numbers)) != len(part_numbers)
        ):
            raise ValidationError(
                "part_numbers must be a unique bounded batch",
                code="invalid_part_batch",
            )
        if any(part < 1 or part > upload.part_count for part in part_numbers):
            raise ValidationError(
                "part number is outside this upload's expected range",
                code="part_number_out_of_range",
            )
        expires_at = datetime.now(UTC) + timedelta(seconds=self._presign_ttl_seconds)
        return [
            SignedPartView(
                part_number=part_number,
                url=await self._store.presign_upload_part(
                    tenant_id=str(self._tenant_id),
                    key=upload.storage_key,
                    provider_upload_id=upload.provider_upload_id,
                    part_number=part_number,
                ),
                expires_at=expires_at,
                required_headers={},
            )
            for part_number in part_numbers
        ]

    async def abort(self, upload_id: UUID) -> bool | None:
        upload = await self._uploads.get_for_owner(upload_id, self._owner_id, lock=True)
        if upload is None:
            return None
        if upload.state is DocumentUploadState.COMPLETED:
            raise ConflictError(
                "A completed upload cannot be aborted.", code="upload_state_conflict"
            )
        if upload.state in {DocumentUploadState.ABORTED, DocumentUploadState.EXPIRED}:
            return True
        await self._store.abort_multipart_upload(
            tenant_id=str(self._tenant_id),
            key=upload.storage_key,
            provider_upload_id=upload.provider_upload_id,
        )
        # A COMPLETING session may have crossed S3's irreversible boundary
        # before crashing. DELETE is idempotent and prevents an unreferenced
        # completed object from surviving terminal expiry.
        await self._store.delete(str(self._tenant_id), upload.storage_key)
        await self._uploads.set_state(upload.id, self._owner_id, DocumentUploadState.ABORTED)
        await self._audit.emit(
            action=AuditAction.DOCUMENT_UPLOAD_ABORTED,
            actor=self._audit_actor,
            resource_type="document_upload",
            resource_id=str(upload.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"document_id": str(upload.document_id)},
        )
        return True

    async def complete(self, upload_id: UUID, parts: list[CompletePartInput]) -> Document | None:
        upload = await self._uploads.get_for_owner(upload_id, self._owner_id, lock=True)
        if upload is None:
            return None
        if upload.state is DocumentUploadState.COMPLETED:
            return await self._documents.get(upload.document_id)
        if self._is_expired(upload):
            raise ConflictError("Upload session has expired.", code="upload_session_expired")
        self._validate_completion_manifest(upload, parts)
        if upload.state is DocumentUploadState.COMPLETING:
            return await self._complete_from_completing(upload, parts)
        if upload.state is not DocumentUploadState.INITIATED:
            raise ConflictError(
                "Upload cannot be completed in its current state.",
                code="upload_state_conflict",
            )

        provider_parts = await self._store.list_multipart_parts(
            tenant_id=str(self._tenant_id),
            key=upload.storage_key,
            provider_upload_id=upload.provider_upload_id,
        )
        self._validate_provider_parts(upload, parts, provider_parts)
        await self._uploads.set_state(upload.id, self._owner_id, DocumentUploadState.COMPLETING)
        # The router commits this boundary before any irreversible provider
        # completion. A crash after S3 completion then resumes from durable
        # COMPLETING and HEAD, never from INITIATED/list_parts (ADR-0023).
        await self._session.commit()
        upload = await self._uploads.get_for_owner(upload_id, self._owner_id, lock=True)
        assert upload is not None
        # Another completer can win while the durable boundary commit releases
        # the row lock. Re-enter the state machine after reacquisition: COMPLETED
        # returns the existing row, COMPLETING performs one HEAD/complete recovery.
        if upload.state is DocumentUploadState.COMPLETED:
            return await self._documents.get(upload.document_id)
        if upload.state is not DocumentUploadState.COMPLETING:
            raise ConflictError(
                "Upload cannot be completed in its current state.",
                code="upload_state_conflict",
            )
        return await self._complete_from_completing(upload, parts, recovered=False)

    async def _complete_from_completing(
        self,
        upload: DocumentUpload,
        parts: list[CompletePartInput],
        *,
        recovered: bool = True,
    ) -> Document:
        """Finish/recover the provider boundary while holding the upload row lock."""
        try:
            stored = await self._store.head(str(self._tenant_id), upload.storage_key)
        except NotFoundError:
            # The durable boundary committed but the provider completion did
            # not: verify the still-live parts before the one irreversible call.
            provider_parts = await self._store.list_multipart_parts(
                tenant_id=str(self._tenant_id),
                key=upload.storage_key,
                provider_upload_id=upload.provider_upload_id,
            )
            self._validate_provider_parts(upload, parts, provider_parts)
            stored = await self._store.complete_multipart_upload(
                tenant_id=str(self._tenant_id),
                key=upload.storage_key,
                provider_upload_id=upload.provider_upload_id,
                parts=[(part.part_number, part.etag) for part in parts],
            )
        return await self._finalize_verified(upload, stored, recovered=recovered)

    async def recover_completing(self, upload_id: UUID) -> Document | None:
        """Finalize a previously completed S3 object from durable COMPLETING."""
        upload = await self._uploads.get_for_owner(upload_id, self._owner_id, lock=True)
        if upload is None:
            return None
        if upload.state is DocumentUploadState.COMPLETED:
            return await self._documents.get(upload.document_id)
        if upload.state is not DocumentUploadState.COMPLETING:
            raise ConflictError(
                "Upload is not awaiting completion recovery.",
                code="upload_state_conflict",
            )
        return await self._recover_completing_upload(upload)

    async def _recover_completing_upload(self, upload: DocumentUpload) -> Document:
        """Recover either side of the durable COMPLETING/provider boundary.

        If S3 completion already happened, HEAD finalizes its document. If the
        process died immediately after committing COMPLETING, the authoritative
        provider part list supplies the manifest: layout was validated before
        that commit, and is revalidated here before completion. No client retry
        payload is required, so a fresh browser can always recover the session.
        """
        try:
            stored = await self._store.head(str(self._tenant_id), upload.storage_key)
        except NotFoundError:
            provider_parts = await self._store.list_multipart_parts(
                tenant_id=str(self._tenant_id),
                key=upload.storage_key,
                provider_upload_id=upload.provider_upload_id,
            )
            self._validate_provider_part_layout(upload, provider_parts)
            stored = await self._store.complete_multipart_upload(
                tenant_id=str(self._tenant_id),
                key=upload.storage_key,
                provider_upload_id=upload.provider_upload_id,
                parts=[(part.part_number, part.etag) for part in provider_parts],
            )
        return await self._finalize_verified(upload, stored, recovered=True)

    async def cleanup_failed_initiation(self, upload: DocumentUpload) -> None:
        """Abort a provider upload when the DB transaction failed to commit."""
        await self._store.abort_multipart_upload(
            tenant_id=str(self._tenant_id),
            key=upload.storage_key,
            provider_upload_id=upload.provider_upload_id,
        )

    async def _view(self, upload: DocumentUpload) -> UploadSessionView:
        document = (
            await self._documents.get(upload.document_id)
            if upload.state is DocumentUploadState.COMPLETED
            else None
        )
        completed: list[UploadedPart] = []
        if upload.state is DocumentUploadState.INITIATED:
            completed = await self._store.list_multipart_parts(
                tenant_id=str(self._tenant_id),
                key=upload.storage_key,
                provider_upload_id=upload.provider_upload_id,
            )
        return UploadSessionView(
            upload=upload,
            completed_parts=tuple(
                UploadPartView(part.part_number, part.etag, part.size_bytes) for part in completed
            ),
            document=document,
        )

    def _is_expired(self, upload: DocumentUpload) -> bool:
        expires_at = upload.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= datetime.now(UTC) and upload.state in {
            DocumentUploadState.INITIATED,
            DocumentUploadState.COMPLETING,
        }

    async def _expire_if_needed(self, upload: DocumentUpload) -> DocumentUpload:
        if not self._is_expired(upload):
            return upload
        await self._store.abort_multipart_upload(
            tenant_id=str(self._tenant_id),
            key=upload.storage_key,
            provider_upload_id=upload.provider_upload_id,
        )
        await self._store.delete(str(self._tenant_id), upload.storage_key)
        expired = await self._uploads.set_state(
            upload.id,
            self._owner_id,
            DocumentUploadState.EXPIRED,
            error="upload_session_expired",
        )
        assert expired is not None
        await self._audit.emit(
            action=AuditAction.DOCUMENT_UPLOAD_EXPIRED,
            actor=self._audit_actor,
            resource_type="document_upload",
            resource_id=str(upload.id),
            outcome=AuditOutcome.ERROR,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"document_id": str(upload.document_id)},
        )
        return expired

    def _validate_provider_parts(
        self,
        upload: DocumentUpload,
        submitted: list[CompletePartInput],
        provider: list[UploadedPart],
    ) -> None:
        self._validate_provider_part_layout(upload, provider)
        for client_part, stored_part in zip(submitted, provider, strict=True):
            if stored_part.etag != client_part.etag:
                raise ValidationError(
                    "submitted parts do not match storage's completed parts",
                    code="part_etag_mismatch",
                )

    def _validate_completion_manifest(
        self, upload: DocumentUpload, parts: list[CompletePartInput]
    ) -> None:
        expected_numbers = list(range(1, upload.part_count + 1))
        if [part.part_number for part in parts] != expected_numbers:
            raise ValidationError(
                "parts must be strictly ascending and contiguous from 1",
                code="invalid_part_manifest",
            )
        if any(not _is_valid_etag(part.etag) for part in parts):
            raise ValidationError(
                "part ETags must be bounded values without control characters",
                code="invalid_part_etag",
            )

    def _validate_provider_part_layout(
        self, upload: DocumentUpload, provider: list[UploadedPart]
    ) -> None:
        if len(provider) != upload.part_count:
            raise ValidationError(
                "storage has an incomplete multipart upload",
                code="incomplete_provider_parts",
            )
        expected_last_size = upload.size_bytes - upload.part_size_bytes * (upload.part_count - 1)
        for index, stored_part in enumerate(provider):
            expected_size = (
                expected_last_size if index == upload.part_count - 1 else upload.part_size_bytes
            )
            if stored_part.part_number != index + 1 or stored_part.size_bytes != expected_size:
                raise ValidationError(
                    "storage has an invalid multipart layout",
                    code="invalid_provider_part_layout",
                )
            if not _is_valid_etag(stored_part.etag):
                raise ValidationError(
                    "storage returned an invalid multipart ETag",
                    code="invalid_provider_part_etag",
                )

    async def _fail_completion(self, upload: DocumentUpload, code: str) -> None:
        await self._uploads.set_state(
            upload.id, self._owner_id, DocumentUploadState.FAILED, error=code
        )
        await self._store.delete(str(self._tenant_id), upload.storage_key)

    async def _finalize_verified(
        self, upload: DocumentUpload, stored: StoredObjectMetadata, *, recovered: bool
    ) -> Document:
        """Recovery helper kept narrow; normal completion performs richer checks."""
        size_bytes = stored.size_bytes
        content_type = normalize_content_type(stored.content_type)
        object_metadata = stored.metadata
        if size_bytes != upload.size_bytes:
            await self._fail_completion(upload, "stored_size_mismatch")
            raise UploadCompletionRejected(
                "Stored object size does not match the initiated upload.",
                status=413,
                code="stored_size_mismatch",
                title="Payload Too Large",
            )
        if (
            content_type != upload.mime_type
            or object_metadata.get("lumen-upload-id") != str(upload.id)
            or object_metadata.get("lumen-document-id") != str(upload.document_id)
        ):
            await self._fail_completion(upload, "stored_metadata_mismatch")
            raise UploadCompletionRejected(
                "Stored object does not match the initiated upload.",
                status=422,
                code="stored_metadata_mismatch",
                title="Unprocessable Entity",
            )
        existing = await self._documents.get(upload.document_id)
        if existing is not None:
            await self._uploads.set_state(upload.id, self._owner_id, DocumentUploadState.COMPLETED)
            return existing
        kind = document_kind_for_content_type(upload.mime_type)
        document = await self._documents.create(
            document_id=upload.document_id,
            owner_id=self._owner_id,
            collection_id=upload.collection_id,
            filename=upload.filename,
            mime_type=upload.mime_type,
            size_bytes=size_bytes,
            storage_key=upload.storage_key,
            acl_enforced=False,
            status=DocumentStatus.PENDING,
            kind=kind,
        )
        await self._uploads.set_state(upload.id, self._owner_id, DocumentUploadState.COMPLETED)
        await self._audit.emit(
            action=AuditAction.DOCUMENT_UPLOADED,
            actor=self._audit_actor,
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "collection_id": str(document.collection_id),
                "filename": document.filename,
                "mime_type": document.mime_type,
                "size_bytes": document.size_bytes,
                "kind": document.kind.value,
                "recovered": recovered,
            },
        )
        self._enqueue_ingestion_after_commit(
            document.id, media=kind in {DocumentKind.AUDIO, DocumentKind.VIDEO}
        )
        return document

    def _enqueue_ingestion_after_commit(self, document_id: UUID, *, media: bool) -> None:
        tenant_id = self._tenant_id

        def _on_commit(_session: object) -> None:
            tasks.enqueue_ingestion(tenant_id, document_id, media=media)

        event.listen(self._session.sync_session, "after_commit", _on_commit, once=True)


class DocumentAccessService:
    """Permission-checked signed object capabilities and transcript reads."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        store: ObjectStore,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
        presign_ttl_seconds: int,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._store = store
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        self._presign_ttl_seconds = presign_ttl_seconds
        self._groups = GroupRepository(session, tenant_id)
        self._transcripts = TranscriptRepository(session, tenant_id)

    async def create_access_url(
        self, document_id: UUID, *, purpose: str
    ) -> AccessCapability | None:
        document = await self._visible(document_id)
        if document is None:
            await self._audit_denial(document_id, operation="access_url")
            return None
        if document.status is not DocumentStatus.READY:
            raise ConflictError(
                "Document has not passed ingestion validation.", code="document_not_ready"
            )
        download_name = document.filename if purpose == "download" else None
        # Capture before URL minting so the advertised capability never outlives
        # the provider signature by the duration of the presign call.
        issued_at = datetime.now(UTC)
        url = await self._store.presign_get(
            str(self._tenant_id),
            document.storage_key,
            download_filename=download_name,
            content_type=document.mime_type,
        )
        action = (
            AuditAction.DOCUMENT_DOWNLOADED
            if purpose == "download"
            else AuditAction.DOCUMENT_VIEWED
        )
        await self._audit.emit(
            action=action,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": document.filename, "form": purpose},
        )
        return AccessCapability(
            url=url,
            document=document,
            purpose=purpose,
            expires_at=issued_at + timedelta(seconds=self._presign_ttl_seconds),
        )

    async def get_transcript(
        self,
        document_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        around_ms: int | None,
    ) -> TranscriptPage | None:
        document = await self._visible(document_id)
        if document is None:
            await self._audit_denial(document_id, operation="transcript")
            return None
        if document.kind not in {DocumentKind.AUDIO, DocumentKind.VIDEO}:
            raise ConflictError("Document is not audio or video.", code="not_media")
        if (
            document.status is not DocumentStatus.READY
            or document.duration_ms is None
            or document.transcription_model is None
        ):
            raise ConflictError("Media transcript is not ready.", code="transcript_not_ready")
        if around_ms is not None and around_ms >= document.duration_ms:
            raise ValidationError(
                "around_ms must be inside the media timeline",
                code="around_ms_out_of_range",
            )
        if cursor is not None and around_ms is not None:
            raise ValidationError("cursor and around_ms cannot be combined")
        after_ordinal = _decode_transcript_cursor(cursor) if cursor is not None else None
        rows = await self._transcripts.list_segments(
            document.id,
            after_ordinal=after_ordinal,
            around_ms=around_ms,
            limit=limit + 1,
        )
        page = rows[:limit]
        next_cursor = _encode_transcript_cursor(page[-1].ordinal) if len(rows) > limit else None
        speakers = await self._transcripts.list_speakers(document.id)
        await self._audit.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": document.filename, "form": "transcript"},
        )
        return TranscriptPage(
            document=document,
            speakers=tuple(speakers),
            items=tuple(page),
            next_cursor=next_cursor,
        )

    async def _visible(self, document_id: UUID) -> Document | None:
        group_ids = await self._groups.group_ids_for_user(self._owner_id)
        allow_set = AllowSet.for_user(
            tenant_id=self._tenant_id,
            user_id=self._owner_id,
            group_ids=group_ids,
        )
        return await get_permitted_document(
            self._session, allow_set=allow_set, document_id=document_id
        )

    async def _audit_denial(self, document_id: UUID, *, operation: str) -> None:
        """Record a content-safe denial without revealing whether the document exists."""
        await self._audit.emit(
            action=AuditAction.PERMISSION_DENIED,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document_id),
            outcome=AuditOutcome.DENIED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "operation": operation,
                "reason_code": "not_found_or_not_permitted",
                "status": 404,
            },
        )


def _encode_transcript_cursor(ordinal: int) -> str:
    return base64.urlsafe_b64encode(str(ordinal).encode()).decode().rstrip("=")


def _decode_transcript_cursor(cursor: str) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("cursor is malformed", code="invalid_cursor") from exc
    if value < 0:
        raise ValidationError("cursor is malformed", code="invalid_cursor")
    return value
