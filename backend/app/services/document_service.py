"""Document upload & lifecycle use-cases — the ``/documents`` surface (#28).

The orchestration layer for document upload, listing, metadata, content
retrieval, and deletion (ADR-0004: ``services/`` compose adapters; routers call
exactly one service). It pairs three adapters behind one use-case object:

* the tenant-scoped ``db/`` ``DocumentRepository`` + ``CollectionRepository``
  (the only SQL) — for the ``Document`` rows and the target-collection check;
* the ``storage/`` ``ObjectStore`` (#22, CC-12) — the **only** object-store
  caller (put / get / delete / presign_get over tenant-prefixed,
  content-addressed keys, with the allowlist + size-cap validation);
* the one ``AuditSink`` (spec 0004 §2.4) — ``document.uploaded`` / ``viewed`` /
  ``downloaded`` / ``deleted``.

It turns the repository's storage-faithful :class:`~app.domain.entities.Document`
into the wire projection the contract requires (``chunk_count`` is computed here,
``0`` until ingestion #21 completes).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repositories) *and* the
caller's ownership (this service). A document — or the target collection on
upload — in another tenant, or owned by another user in the same tenant, is
treated as **non-existent**: the read/op returns ``None``/``False`` and the
router maps that to **404** (existence non-disclosure; never 403). The
``owner_id``/``tenant_id`` of a created document come from the resolved
principal, never from request input.

**Upload validation (contract 413/415, reuses #22).** The declared content-type
and byte size are checked against the config allowlist/cap via the storage
``validate_upload`` (the single owner of those rules); a rejection is re-mapped
here to the precise HTTP status the contract pins — ``upload_too_large`` → 413,
``content_type_not_allowed`` → 415, ``empty_upload`` → 422 — before any bytes are
stored or any row is written.

**Ingestion seam (#21, OUT of scope here).** A successful upload leaves the
document ``status=pending`` and marks a clear TODO to enqueue the async
parse → chunk → embed task; that Celery task is issue #21 and is *not*
implemented in this change.

Cursor pagination mirrors ``collections_service``: a keyset over
``(created_at, id)`` descending, the opaque cursor carrying only the boundary id
(the repository resolves its ``created_at`` in-database); a malformed cursor is
rejected fail-closed (422).
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.db.repositories import ChunkRepository, CollectionRepository, DocumentRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Document, DocumentStatus
from app.services.audit import AuditSink
from app.storage import ObjectStore
from app.storage.validation import validate_upload

# Pagination bounds mirror the contract's Limit parameter (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# A short, stable cursor prefix so a decoded payload is recognisably one of ours.
_CURSOR_PREFIX = "doc:"


@dataclass(frozen=True, slots=True)
class DocumentView:
    """A document projected for the wire (contract ``Document`` schema).

    The storage-faithful :class:`~app.domain.entities.Document` plus the computed
    ``chunk_count``. The router serialises this into the response model; the
    service never imports a Pydantic/HTTP type.
    """

    document: Document
    chunk_count: int


@dataclass(frozen=True, slots=True)
class DocumentPage:
    """One page of documents plus the opaque cursor for the next page."""

    items: list[DocumentView]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class DocumentContent:
    """The bytes of a document plus the metadata needed to serve them.

    Returned by the inline-content path; the router streams ``data`` as
    ``application/octet-stream``.
    """

    filename: str
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class DocumentText:
    """The extracted plain text of a ready document (contract ``DocumentText``).

    ``text`` is the ingestion parser's output reassembled exactly from the
    stored chunks (#244); ``truncated`` flags an over-cap document cut at the
    ``DOCUMENT_TEXT_MAX_BYTES`` limit.
    """

    text: str
    chunk_count: int
    truncated: bool


class _ChunkSpan(Protocol):
    """The span shape reassembly needs — satisfied by both the domain
    :class:`~app.domain.entities.Chunk` and the chunker's ``TextChunk``."""

    @property
    def text(self) -> str: ...
    @property
    def char_start(self) -> int: ...
    @property
    def char_end(self) -> int: ...


def reassemble_chunk_texts(chunks: Iterable[_ChunkSpan]) -> str:
    """Rebuild the document's readable text from its stored chunks.

    Chunks are produced by a sliding window with **overlap** (``chunk.text ==
    source[char_start:char_end]``, adjacent chunks sharing ``overlap`` chars —
    ``app.ingestion.chunking``), so naive concatenation would duplicate every
    overlap. Walk the chunks in document order and append only the part of each
    chunk past the furthest character already emitted.

    This reproduces the parser output **except for whitespace-only windows the
    chunker deliberately drops** (``chunking.chunk_text`` skips a window whose
    ``strip()`` is empty), so a long run of blank lines between two paragraphs is
    collapsed. That is exactly what a human-readable text preview wants — it is
    NOT a byte-exact inverse of the original file. Pure; unit-tested against the
    real chunker for both the lossless case and the dropped-blank-run case.
    """
    parts: list[str] = []
    prev_end = 0
    for chunk in chunks:
        if chunk.char_end <= prev_end:
            continue  # fully covered by what we already emitted
        skip = max(prev_end - chunk.char_start, 0)
        parts.append(chunk.text[skip:])
        prev_end = chunk.char_end
    return "".join(parts)


# --- Cursor codec (opaque; carries the boundary row id) ---------------------


def _encode_cursor(document_id: UUID) -> str:
    """Encode a boundary document id as an opaque URL-safe cursor."""
    raw = f"{_CURSOR_PREFIX}{document_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    """Decode an opaque cursor back into the boundary document id.

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
    """Clamp the requested page size into the contract's [1, 100] band."""
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


def _map_upload_validation_error(exc: ValidationError) -> Exception:
    """Re-map a storage upload-validation error to the contract's HTTP status.

    The storage ``validate_upload`` (#22) is the single owner of the allowlist +
    size-cap rules and raises a generic ``ValidationError`` (422) with a stable
    ``code``. The contract pins distinct statuses for the upload negatives, so
    map by code here: ``upload_too_large`` → 413, ``content_type_not_allowed`` →
    415; an empty/other rejection stays 422 (INV-8).
    """
    if exc.code == "upload_too_large":
        return PayloadTooLargeError(exc.detail, code=exc.code)
    if exc.code == "content_type_not_allowed":
        return UnsupportedMediaTypeError(exc.detail, code=exc.code)
    return exc


class DocumentService:
    """Upload / list / get / content / delete documents for one principal.

    Constructed per-request with the session, the resolved ``tenant_id`` and
    ``owner_id`` (both from the token — never request input), the object-store
    adapter, and the audit sink + correlation context the router supplies. All
    ownership/tenancy enforcement lives here; the router only (de)serialises and
    maps ``None``/``False`` → 404.
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
        upload_allowed_content_types: frozenset[str],
        max_upload_bytes: int,
    ) -> None:
        self._session = session
        self._documents = DocumentRepository(session, tenant_id)
        self._collections = CollectionRepository(session, tenant_id)
        self._chunks = ChunkRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._store = object_store
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip
        self._allowed_content_types = upload_allowed_content_types
        self._max_upload_bytes = max_upload_bytes

    # --- internal helpers ---------------------------------------------------

    def _owns(self, document: Document) -> bool:
        """Deny-by-default ownership check (spec 0004 §2.2, INV-2)."""
        return document.owner_id == self._owner_id

    async def _view(self, document: Document) -> DocumentView:
        count = await self._documents.count_chunks(document.id)
        return DocumentView(document=document, chunk_count=count)

    async def _visible(self, document_id: UUID) -> Document | None:
        """Fetch a document the caller may see, or ``None`` (→ 404).

        ``None`` for a missing id, a foreign-tenant id (the repository sees no
        row), *or* a same-tenant document owned by another user (ownership
        check) — INV-1/INV-2 collapse all three to 404 at the router.
        """
        document = await self._documents.get(document_id)
        if document is None or not self._owns(document):
            return None
        return document

    # --- use-cases ----------------------------------------------------------

    async def upload(
        self,
        *,
        collection_id: UUID,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> DocumentView | None:
        """Validate, store, and register an uploaded document.

        Order matters for fail-closed correctness:

        1. **Visibility** of the target collection first — a collection in
           another tenant or owned by another user is reported as ``None`` → 404
           (INV-1/INV-2) *before* any bytes touch storage.
        2. **Validation** of the declared content-type + size against the config
           allowlist/cap (the #22 storage rules), re-mapped to 415/413/422.
        3. **Store** the bytes via the #22 ``ObjectStore`` (tenant-prefixed,
           content-addressed) — the only object-store caller.
        4. **Register** a ``Document`` row ``status=pending`` owned by the caller.
        5. **Audit** ``document.uploaded`` through the one sink (INV-6).

        Returns ``None`` when the target collection is not the caller's (→ 404);
        the ``owner_id``/``tenant_id`` come from the principal, never the request.

        Ingestion (#21) is **not** triggered here — see the TODO below.
        """
        # 1. Target collection must be the caller's (deny by default; INV-1/INV-2).
        collection = await self._collections.get(collection_id)
        if collection is None or collection.owner_id != self._owner_id:
            return None

        # 2. Allowlist + size cap (reuse #22's single rule owner), mapped to the
        #    contract's distinct statuses (415/413/422).
        try:
            validate_upload(
                size_bytes=len(data),
                content_type=content_type,
                allowed_content_types=self._allowed_content_types,
                max_bytes=self._max_upload_bytes,
            )
        except ValidationError as exc:
            raise _map_upload_validation_error(exc) from exc

        # 3. Store the bytes via the #22 adapter (the only object-store caller).
        stored = await self._store.put(
            tenant_id=str(self._tenant_id),
            data=data,
            content_type=content_type,
            filename=filename,
        )

        # 4. Register the row, owned by the caller, pending ingestion.
        document = await self._documents.create(
            owner_id=self._owner_id,
            collection_id=collection_id,
            filename=filename,
            mime_type=content_type,
            size_bytes=stored.size_bytes,
            storage_key=stored.key,
            status=DocumentStatus.PENDING,
        )

        # 5. Audit the upload (INV-6) — flushes within this request's transaction.
        await self._audit.emit(
            action=AuditAction.DOCUMENT_UPLOADED,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "collection_id": str(collection_id),
                "filename": document.filename,
                "mime_type": document.mime_type,
                "size_bytes": document.size_bytes,
            },
        )

        # Enqueue ingestion (#21, CC-5): the worker drives the pending row through
        # parse → chunk → embed → persist to ``ready`` (or ``failed`` with a
        # reason). Enqueue is the single seam in ``tasks/`` (ADR-0004: only
        # ``tasks/`` enqueues). Fire it **after** this request's transaction
        # commits — otherwise the worker could pick up the message and not yet see
        # the row (it would no-op and the document would stick at ``pending``). An
        # after-commit session hook makes the enqueue exactly-once on success and
        # never on rollback; the task is idempotent/defensive as a backstop.
        self._enqueue_ingestion_after_commit(document.id)

        # A freshly uploaded document holds no chunks yet.
        return DocumentView(document=document, chunk_count=0)

    def _enqueue_ingestion_after_commit(self, document_id: UUID) -> None:
        """Schedule the ingestion enqueue to fire after the request commits.

        Registers a one-shot SQLAlchemy ``after_commit`` listener on this
        request's session so the Celery message is sent only once the ``pending``
        document row is durably committed (and never if the transaction rolls
        back). Enqueueing goes through ``tasks.enqueue_ingestion`` — the single
        enqueue point (ADR-0004). ``once=True`` lets SQLAlchemy remove the
        listener safely after it fires (removing it from inside the callback
        would mutate the listener collection mid-dispatch); ``import app.tasks``
        is resolved at call time so a test can monkeypatch the enqueue.
        """
        # Imported lazily so the service module does not pull in Celery at import
        # time (keeps unit tests that exercise the pure helpers dependency-light).
        from sqlalchemy import event

        import app.tasks as tasks

        tenant_id = self._tenant_id

        def _on_commit(_session: object) -> None:
            tasks.enqueue_ingestion(tenant_id, document_id)

        event.listen(self._session.sync_session, "after_commit", _on_commit, once=True)

    def _enqueue_index_sync_after_commit(self, document_id: UUID) -> None:
        """Schedule a search-index sync to fire after the request commits.

        The deletion twin of :meth:`_enqueue_ingestion_after_commit` (same
        one-shot ``after_commit`` listener, same single enqueue seam in
        ``tasks/``): the ``lumen.sync_document_index`` worker reads the
        committed (deleted) state and removes the document's chunk docs from
        the engine (ADR-0010 §5) — and never fires on rollback.
        """
        from sqlalchemy import event

        import app.tasks as tasks

        tenant_id = self._tenant_id

        def _on_commit(_session: object) -> None:
            tasks.enqueue_index_sync(tenant_id, document_id)

        event.listen(self._session.sync_session, "after_commit", _on_commit, once=True)

    async def list_page(
        self,
        *,
        cursor: str | None,
        limit: int | None,
        collection_id: UUID | None = None,
        status: DocumentStatus | None = None,
        filename_query: str | None = None,
    ) -> DocumentPage:
        """Return one keyset page of the caller's own documents, optionally filtered.

        Owner- and tenant-scoped (deny by default). Fetches ``limit + 1`` rows to
        decide whether a next page exists without a second round-trip; the extra
        row (if present) determines ``next_cursor`` and is then dropped. The
        optional ``collection_id`` / ``status`` / ``filename_query`` narrow the
        result (the contract's list filters).
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._documents.list_for_owner_page(
            self._owner_id,
            limit=page_size + 1,
            after_id=after_id,
            collection_id=collection_id,
            status=status,
            filename_query=filename_query,
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        items = [await self._view(d) for d in page]
        return DocumentPage(items=items, next_cursor=next_cursor)

    async def get(self, document_id: UUID) -> DocumentView | None:
        """Fetch one of the caller's documents, or ``None`` if not visible (→ 404)."""
        document = await self._visible(document_id)
        if document is None:
            return None
        return await self._view(document)

    async def get_content(self, document_id: UUID) -> DocumentContent | None:
        """Read the stored bytes for one of the caller's documents (→ 404 if not).

        Establishes visibility first (INV-1/INV-2), then reads the bytes back via
        the #22 ``ObjectStore`` (tenant-prefix checked inside the adapter). Audits
        ``document.downloaded`` (INV-6). Returns ``None`` when the document is not
        the caller's.
        """
        document = await self._visible(document_id)
        if document is None:
            return None
        data = await self._store.get(str(self._tenant_id), document.storage_key)
        await self._audit.emit(
            action=AuditAction.DOCUMENT_DOWNLOADED,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": document.filename},
        )
        return DocumentContent(
            filename=document.filename,
            mime_type=document.mime_type,
            data=data,
        )

    async def get_text(self, document_id: UUID, *, max_bytes: int) -> DocumentText | None:
        """Serve the extracted plain text of a ready document (#244).

        Visibility first (INV-1/INV-2): not the caller's → ``None`` → 404 at
        the router, indistinguishable from missing. A **visible** document that
        is not ``ready`` has no text yet → :class:`ConflictError`
        (``document_not_ready`` → 409, INV-8's illegal-state arm). The text is
        the ingestion parser output reassembled exactly from the stored chunks
        (:func:`reassemble_chunk_texts` — overlap-aware), capped at
        ``max_bytes`` UTF-8 bytes on a character boundary with ``truncated``
        set. Audited ``document.viewed`` (INV-6).
        """
        document = await self._visible(document_id)
        if document is None:
            return None
        if document.status is not DocumentStatus.READY:
            raise ConflictError(
                "Document has not finished ingestion; no text is available yet.",
                code="document_not_ready",
            )
        chunks = await self._chunks.list_for_document(document_id)
        text = reassemble_chunk_texts(chunks)
        truncated = False
        encoded = text.encode("utf-8")
        if len(encoded) > max_bytes:
            # Cut on a byte budget, then drop any half character at the edge.
            text = encoded[:max_bytes].decode("utf-8", errors="ignore")
            truncated = True
        await self._audit.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": document.filename, "form": "text"},
        )
        return DocumentText(text=text, chunk_count=len(chunks), truncated=truncated)

    async def presign_content(self, document_id: UUID) -> str | None:
        """Mint a short-TTL presigned GET URL for one of the caller's documents.

        Visibility-checked first (INV-1/INV-2 → 404 via ``None``). The URL comes
        from the #22 ``ObjectStore.presign_get`` (tenant-prefix checked inside the
        adapter); the client follows the router's 302 ``Location`` to transfer the
        bytes directly from storage, not through the API process. Audits
        ``document.downloaded`` (INV-6).
        """
        document = await self._visible(document_id)
        if document is None:
            return None
        url = await self._store.presign_get(str(self._tenant_id), document.storage_key)
        await self._audit.emit(
            action=AuditAction.DOCUMENT_DOWNLOADED,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"filename": document.filename, "presigned": True},
        )
        return url

    async def delete(self, document_id: UUID) -> bool:
        """Delete one of the caller's documents: the row (+ chunks) and the object.

        Visibility (tenant + ownership) is established *before* any write so a
        non-owner's document is never touched and is reported as 404 (INV-1/INV-2),
        not 403. On success the row is deleted (the ORM ``delete-orphan`` cascade
        removes its chunks) **and** the stored object is removed via the #22
        adapter, then ``document.deleted`` is audited (INV-6). Returns ``False``
        when the document is not visible to the caller.

        Order: delete the row first, ``flush`` it, then remove the object **only
        if no other document (this tenant) still references the content-addressed
        key** (#269). Objects are keyed by ``{tenant}/{sha256}/{filename}``, so two
        identical uploads share one object — deleting it unconditionally would
        break the survivor's ``/content``. The flush is required because the app
        sessionmaker runs ``autoflush=False`` (``db/session.py``); without it the
        ``count_by_storage_key`` read would still see the just-deleted row. All of
        this commits within this request's transaction at the router.
        """
        document = await self._visible(document_id)
        if document is None:
            return False
        deleted = await self._documents.delete(document_id)
        if not deleted:  # pragma: no cover — visibility already established
            return False
        await self._session.flush()
        if await self._documents.count_by_storage_key(document.storage_key) == 0:
            await self._store.delete(str(self._tenant_id), document.storage_key)
        # Clear the document's chunk docs from the search index (ADR-0010 §5):
        # enqueued after-commit so the worker reads the already-deleted state and
        # the index never resurrects a rolled-back delete. Best-effort — the
        # index is derived; the reindex command repairs a missed sync.
        self._enqueue_index_sync_after_commit(document.id)
        await self._audit.emit(
            action=AuditAction.DOCUMENT_DELETED,
            actor=AuditActor.user(self._owner_id),
            resource_type="document",
            resource_id=str(document.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "collection_id": str(document.collection_id),
                "filename": document.filename,
            },
        )
        return True
