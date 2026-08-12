"""Artifact store use-cases — agent/run-produced files (issue #208, CC-12).

The orchestration layer for the **artifact store**: files an agent/run *produces*
(distinct from user-uploaded ``documents``) — the persistence seam the
file-writing tool (F-TOOL-FILE) and the code sandbox's output capture (F-PY-1)
write through. Writing an artifact is a **T1** action ([spec 0004 §2.5] —
owner-gated, audited, no extra approval). This service is the **testable seam**:
there is deliberately no HTTP route in this issue — the artifact panel UI
(F-TOOL-FILE-UI) and the tool/sandbox callers are follow-ups; the negatives below
drive the service directly.

It pairs three adapters behind one use-case object (ADR-0004: ``services/``
compose adapters):

* the tenant-scoped ``db/`` :class:`~app.db.repositories.ArtifactRepository` (the
  only SQL) — for the immutable ``Artifact`` rows;
* the ``storage/`` :class:`~app.storage.object_store.ObjectStore` (#22, CC-12) —
  the **only** object-store caller (``put_artifact`` / ``get_artifact`` /
  ``delete_artifact`` / ``presign_get_artifact`` over tenant-prefixed,
  content-addressed keys under the ``artifacts/`` prefix, with the artifact
  allowlist + size-cap validation);
* the one :class:`~app.services.audit.AuditSink` (spec 0004 §2.4) —
  ``artifact.created`` / ``downloaded`` / ``deleted``.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repository) *and* the
caller's ownership (this service). An artifact in another tenant, or owned by
another user in the same tenant, is treated as **non-existent**: the read/op
returns ``None``/``False`` and the caller maps that to **404** (existence
non-disclosure; never 403). The ``owner_id``/``tenant_id`` of a created artifact
come from the resolved principal, never from request input. (Owner-*or-grant*
visibility is the modelled end-state — the ``grants`` table's CHECK constraint
admits only ``collection``/``document`` resources today, so widening artifact
visibility to explicit grants is a deliberate follow-up; :meth:`_visible` is the
single chokepoint that widening would extend.)

**Validation (#208 AC-2).** The declared content-type and byte size are checked
against the **artifact** allowlist/cap (distinct from uploads; ``MAX_ARTIFACT_BYTES``
/ ``ARTIFACT_ALLOWED_CONTENT_TYPES``) via the storage ``validate_upload`` (the
single owner of those rules) — a rejection is a typed ``ValidationError`` (422),
raised **before** any bytes are stored or any row is written.

**Retention (#208 §6).** When ``ARTIFACT_RETENTION_DAYS`` is set, ``create``
stamps ``retention_expires_at = now + N days``; ``None`` ⇒ keep forever. The
retention janitor that purges expired rows is a stub in this issue
(:mod:`app.tasks.artifact_retention`).

Cursor pagination mirrors ``document_service``: a keyset over ``(created_at,
id)`` descending, the opaque cursor carrying only the boundary id; a malformed
cursor is rejected fail-closed (422).

[spec 0004 §2.5]: docs/specs/0004-security-and-domain-invariants.md
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.db.repositories import ArtifactRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import Artifact, ArtifactProducedBy, AuditOutcome
from app.services.audit import AuditSink
from app.storage import ObjectStore
from app.storage.validation import validate_upload

# Pagination bounds mirror the documents/collections list contract (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# A short, stable cursor prefix so a decoded payload is recognisably one of ours.
_CURSOR_PREFIX = "art:"


@dataclass(frozen=True, slots=True)
class ArtifactLinks:
    """The producing-context back-links for a new artifact (issue #208).

    At most the one matching ``produced_by`` is meaningful, but all three are
    carried so a tool invocation inside a chat session can record both. The
    service does not (yet) validate that a link id exists — the run/tool tables
    (CC-A) are not built; the ``session_id`` FK is the only DB-enforced link.
    """

    session_id: UUID | None = None
    run_id: UUID | None = None
    tool_invocation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    """One page of artifacts plus the opaque cursor for the next page."""

    items: list[Artifact]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """The bytes of an artifact plus the metadata needed to serve them.

    Returned by the inline-content path; a caller streams ``data`` as the
    artifact's ``mime_type``.
    """

    filename: str
    mime_type: str
    data: bytes


# --- Cursor codec (opaque; carries the boundary row id) ---------------------


def _encode_cursor(artifact_id: UUID) -> str:
    """Encode a boundary artifact id as an opaque URL-safe cursor."""
    raw = f"{_CURSOR_PREFIX}{artifact_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    """Decode an opaque cursor back into the boundary artifact id.

    Raises:
        ValidationError: the cursor is not one this server issued (malformed
            base64, missing prefix, or non-uuid payload). Fail-closed → 422
            rather than silently returning the first page (INV-8).
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    """Clamp the requested page size into the [1, 100] band."""
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


class ArtifactsService:
    """Create / list / get / download / delete artifacts for one principal.

    Constructed per-request with the session, the resolved ``tenant_id`` and
    ``owner_id`` (both from the token — never request input), the object-store
    adapter, the audit sink + correlation context, and the artifact
    allowlist/cap + retention window from config. All ownership/tenancy
    enforcement lives here; a caller only (de)serialises and maps
    ``None``/``False`` → 404.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        object_store: ObjectStore,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
        artifact_allowed_content_types: frozenset[str],
        max_artifact_bytes: int,
        retention_days: int | None = None,
    ) -> None:
        self._session = session
        self._artifacts = ArtifactRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._store = object_store
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        self._allowed_content_types = artifact_allowed_content_types
        self._max_artifact_bytes = max_artifact_bytes
        self._retention_days = retention_days

    # --- internal helpers ---------------------------------------------------

    def _owns(self, artifact: Artifact) -> bool:
        """Deny-by-default ownership check (spec 0004 §2.2, INV-2)."""
        return artifact.owner_id == self._owner_id

    async def _visible(self, artifact_id: UUID) -> Artifact | None:
        """Fetch an artifact the caller may see, or ``None`` (→ 404).

        ``None`` for a missing id, a foreign-tenant id (the repository sees no
        row), *or* a same-tenant artifact owned by another user (ownership
        check) — INV-1/INV-2 collapse all three to 404 at the caller. This is the
        single chokepoint a future owner-*or-grant* widening would extend.
        """
        artifact = await self._artifacts.get(artifact_id)
        if artifact is None or not self._owns(artifact):
            return None
        return artifact

    def _retention_expires_at(self, now: datetime) -> datetime | None:
        """Compute the retention boundary from config (``None`` ⇒ keep forever)."""
        if self._retention_days is None:
            return None
        return now + timedelta(days=self._retention_days)

    # --- use-cases ----------------------------------------------------------

    async def create_artifact(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        produced_by: ArtifactProducedBy,
        links: ArtifactLinks | None = None,
    ) -> Artifact:
        """Validate, store, and register a produced artifact (#208 AC-1/AC-2/AC-4).

        Order matters for fail-closed correctness:

        1. **Validate** the declared content-type + size against the artifact
           allowlist/cap (distinct from uploads) — a rejection is a 422
           ``ValidationError`` raised **before** any bytes touch storage (AC-2).
        2. **Store** the bytes via the #22 ``ObjectStore.put_artifact``
           (tenant-prefixed, content-addressed under ``artifacts/``) — the only
           object-store caller.
        3. **Register** an immutable ``Artifact`` row owned by the caller, with the
           retention boundary stamped from config.
        4. **Audit** ``artifact.created`` through the one sink (INV-6).

        The ``owner_id``/``tenant_id`` come from the principal, never the request;
        ``produced_by`` + ``links`` come from the producing context (the tool/run).
        """
        # 1. Artifact allowlist + size cap (reuse the single rule owner). A
        #    rejection stays a 422 ValidationError (#208 AC-2 pins 422 for both
        #    over-cap and disallowed-type — unlike the upload path's 413/415 split).
        validate_upload(
            size_bytes=len(data),
            content_type=content_type,
            allowed_content_types=self._allowed_content_types,
            max_bytes=self._max_artifact_bytes,
        )

        # 2. Store the bytes via the #22 adapter (the only object-store caller).
        stored = await self._store.put_artifact(
            tenant_id=str(self._tenant_id),
            data=data,
            content_type=content_type,
            filename=filename,
        )

        # 3. Register the immutable row, owned by the caller, with retention.
        now = datetime.now(UTC)
        link = links or ArtifactLinks()
        artifact = await self._artifacts.create(
            owner_id=self._owner_id,
            produced_by=produced_by,
            filename=filename,
            mime_type=content_type,
            size_bytes=stored.size_bytes,
            storage_key=stored.key,
            sha256=stored.sha256,
            session_id=link.session_id,
            run_id=link.run_id,
            tool_invocation_id=link.tool_invocation_id,
            retention_expires_at=self._retention_expires_at(now),
        )

        # 4. Audit the creation (INV-6) — flushes within this request's transaction.
        await self._audit.emit(
            action=AuditAction.ARTIFACT_CREATED,
            actor=AuditActor.user(self._owner_id),
            resource_type="artifact",
            resource_id=str(artifact.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "filename": artifact.filename,
                "mime_type": artifact.mime_type,
                "size_bytes": artifact.size_bytes,
                "produced_by": artifact.produced_by.value,
            },
        )
        return artifact

    async def list_artifacts(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        produced_by: ArtifactProducedBy | None = None,
        session_id: UUID | None = None,
    ) -> ArtifactPage:
        """Return one keyset page of the caller's own artifacts, optionally filtered.

        Owner- *and* tenant-scoped (deny by default). Fetches ``limit + 1`` rows to
        decide whether a next page exists without a second round-trip; the extra
        row (if present) determines ``next_cursor`` and is then dropped. The
        optional ``produced_by`` / ``session_id`` narrow the result.
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._artifacts.list_for_owner_page(
            self._owner_id,
            limit=page_size + 1,
            after_id=after_id,
            produced_by=produced_by,
            session_id=session_id,
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return ArtifactPage(items=page, next_cursor=next_cursor)

    async def get_artifact(self, artifact_id: UUID) -> Artifact | None:
        """Fetch one of the caller's artifacts, or ``None`` if not visible (→ 404)."""
        return await self._visible(artifact_id)

    async def get_artifact_content(self, artifact_id: UUID) -> ArtifactContent | None:
        """Read the stored bytes for one of the caller's artifacts (→ 404 if not).

        Establishes visibility first (INV-1/INV-2), then reads the bytes back via
        the #22 ``ObjectStore.get_artifact`` (tenant-prefix checked inside the
        adapter). Audits ``artifact.downloaded`` (INV-6). Returns ``None`` when the
        artifact is not the caller's.
        """
        artifact = await self._visible(artifact_id)
        if artifact is None:
            return None
        data = await self._store.get_artifact(str(self._tenant_id), artifact.storage_key)
        await self._audit.emit(
            action=AuditAction.ARTIFACT_DOWNLOADED,
            actor=AuditActor.user(self._owner_id),
            resource_type="artifact",
            resource_id=str(artifact.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": artifact.filename},
        )
        return ArtifactContent(
            filename=artifact.filename,
            mime_type=artifact.mime_type,
            data=data,
        )

    async def presign_artifact_content(self, artifact_id: UUID) -> str | None:
        """Mint a short-TTL presigned GET URL for one of the caller's artifacts (AC-1).

        Visibility-checked first (INV-1/INV-2 → 404 via ``None``). The URL comes
        from the #22 ``ObjectStore.presign_get_artifact`` (tenant-prefix checked
        inside the adapter); a client follows it to transfer the bytes directly
        from storage, not through the API process. Audits ``artifact.downloaded``
        (INV-6). Returns ``None`` when the artifact is not the caller's.
        """
        artifact = await self._visible(artifact_id)
        if artifact is None:
            return None
        url = await self._store.presign_get_artifact(str(self._tenant_id), artifact.storage_key)
        await self._audit.emit(
            action=AuditAction.ARTIFACT_DOWNLOADED,
            actor=AuditActor.user(self._owner_id),
            resource_type="artifact",
            resource_id=str(artifact.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": artifact.filename, "presigned": True},
        )
        return url

    async def delete_artifact(self, artifact_id: UUID) -> bool:
        """Delete one of the caller's artifacts: the object then the row (#208 AC-4).

        Visibility (tenant + ownership) is established *before* any write so a
        non-owner's artifact is never touched and is reported as 404 (INV-1/INV-2),
        not 403. On success the stored object is removed via the #22 adapter, then
        the row, then ``artifact.deleted`` is audited (INV-6). Returns ``False``
        when the artifact is not visible to the caller.

        Order: the object is deleted first (idempotent on S3/MinIO — a missing key
        is a no-op), then the row, so a row never outlives its bytes silently; both
        commit within this request's transaction at the caller.
        """
        artifact = await self._visible(artifact_id)
        if artifact is None:
            return False
        await self._store.delete_artifact(str(self._tenant_id), artifact.storage_key)
        deleted = await self._artifacts.delete(artifact_id)
        if not deleted:  # pragma: no cover — visibility already established
            return False
        await self._audit.emit(
            action=AuditAction.ARTIFACT_DELETED,
            actor=AuditActor.user(self._owner_id),
            resource_type="artifact",
            resource_id=str(artifact.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": artifact.filename},
        )
        return True
