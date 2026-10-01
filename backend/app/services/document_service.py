"""Document metadata and lifecycle use-cases — the v1 ``/documents`` surface.

The orchestration layer for listing, metadata, extracted-text preview, and
deletion (ADR-0004: ``services/`` compose adapters; routers call exactly one
service). Direct upload and signed access live in
``services.document_upload_service`` (spec 0008); this class deliberately has
no whole-file byte ingress or egress method.

* the tenant-scoped ``db/`` repositories (the only SQL);
* the ``storage/`` ``ObjectStore`` (#22, CC-12) — the **only** object-store
  caller for deletion and signed legacy helpers;
* the one ``AuditSink`` (spec 0004 §2.4) — ``document.viewed`` /
  ``downloaded`` / ``deleted``.

It turns the repository's storage-faithful :class:`~app.domain.entities.Document`
into the wire projection the contract requires (``chunk_count`` is computed here,
``0`` until ingestion #21 completes).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repositories) *and* the
caller's ownership/permission (this service). A document in another tenant, or
one the caller may not read/manage in the same tenant, is
treated as **non-existent**: the read/op returns ``None``/``False`` and the
router maps that to **404** (existence non-disclosure; never 403).

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

from app.core.errors import ConflictError, ValidationError
from app.db.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentUploadRepository,
    GroupRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Document, DocumentStatus
from app.retrieval.permissions import AllowSet
from app.retrieval.queries import get_permitted_document, permitted_document_ids
from app.services.audit import AuditSink
from app.storage import ObjectStore

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


class DocumentService:
    """List / get / preview-text / delete documents for one principal.

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
    ) -> None:
        self._session = session
        self._documents = DocumentRepository(session, tenant_id)
        self._uploads = DocumentUploadRepository(session, tenant_id)
        self._chunks = ChunkRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._groups = GroupRepository(session, tenant_id)
        # The requester's read allow-set — the SAME object retrieval keys its
        # permission predicate off (ADR-0019 §2 mode split, spec 0004 §2.2).
        # Resolved lazily because the group half needs a read (ADR-0022 §5);
        # memoized for this service instance, which is per-request, so a group
        # removal still takes effect on the next request.
        self._allow_set_cache: AllowSet | None = None
        self._store = object_store
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    # --- internal helpers ---------------------------------------------------

    async def _resolve_allow_set(self) -> AllowSet:
        """The requester's read allow-set, including their group principals.

        A user in no groups resolves to an empty group set, which narrows the
        allow-set to ownership plus their own user grants — fail closed.
        """
        if self._allow_set_cache is None:
            group_ids = await self._groups.group_ids_for_user(self._owner_id)
            self._allow_set_cache = AllowSet.for_user(
                tenant_id=self._tenant_id, user_id=self._owner_id, group_ids=group_ids
            )
        return self._allow_set_cache

    def _owns(self, document: Document) -> bool:
        """Deny-by-default ownership check (spec 0004 §2.2, INV-2)."""
        return document.owner_id == self._owner_id

    async def _view(self, document: Document) -> DocumentView:
        count = await self._documents.count_chunks(document.id)
        return DocumentView(document=document, chunk_count=count)

    async def _visible(self, document_id: UUID) -> Document | None:
        """Fetch a document the caller may **read**, or ``None`` (→ 404).

        Delegates to the permission chokepoint
        (:func:`app.retrieval.queries.get_permitted_document`) so this surface
        applies the identical mode-split predicate retrieval does — there is no
        second, weaker rule for point reads:

        * ``acl_enforced = false`` (uploads, ``web``): owner **or** an explicit
          grant, exactly as spec 0004 §2.2 defines "permitted to see";
        * ``acl_enforced = true`` (managed-connector documents): a **fresh
          mirrored-principal intersection and nothing else**. Connector rows are
          owned by the *connecting admin*, so an ownership-only check here
          handed that admin every mirrored file — including ones with an empty,
          stale, or non-member ACL mirror. It no longer does.

        ``None`` for a missing id, a foreign-tenant id (the query sees no row),
        or a document the requester is not permitted — INV-1/INV-2 collapse all
        three to 404 at the router.
        """
        return await get_permitted_document(
            self._session, allow_set=await self._resolve_allow_set(), document_id=document_id
        )

    async def _owned(self, document_id: UUID) -> Document | None:
        """Fetch a document the caller **manages**, or ``None`` (→ 404).

        Document *management* (delete) stays an ownership decision and is
        deliberately NOT routed through the read predicate: a grantee must not
        be able to delete the owner's document, and the connecting admin must
        stay able to remove a mirrored document they cannot read (the same
        owner/admin rule that governs the source itself — ADR-0019 §1). Reads
        go through :meth:`_visible`.
        """
        document = await self._documents.get(document_id)
        if document is None or not self._owns(document):
            return None
        return document

    # --- use-cases ----------------------------------------------------------

    def _enqueue_index_sync_after_commit(self, document_id: UUID) -> None:
        """Schedule a search-index sync to fire after the request commits.

        A one-shot ``after_commit`` listener uses the single enqueue seam in
        ``tasks/``: the ``lumen.sync_document_index`` worker reads the
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

        The page is then passed through the **same mode-split predicate** as
        every other read (:func:`app.retrieval.queries.permitted_document_ids`,
        one bounded query over at most a page of ids): owning a document is not
        sufficient for an ``acl_enforced`` connector row, so a listing must not
        surface mirrored documents the requester's mirror does not currently
        admit. Filtering happens **after** the cursor is computed, so paging
        stays complete and monotonic — a page may simply carry fewer items.
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
        permitted = await permitted_document_ids(
            self._session,
            allow_set=await self._resolve_allow_set(),
            document_ids=[d.id for d in page],
        )
        visible = [d for d in page if d.id in permitted]
        # One grouped COUNT for the whole page (#526). Resolved per row this was
        # a serial aggregate over ``chunks`` — the largest table — for every
        # document in the page, on a view users hit constantly. A document with
        # no chunks is absent from the mapping and defaults to 0, exactly as the
        # single-id count returns for a document ingestion has not populated yet.
        chunk_counts = await self._documents.count_chunks_for([d.id for d in visible])
        items = [DocumentView(document=d, chunk_count=chunk_counts.get(d.id, 0)) for d in visible]
        return DocumentPage(items=items, next_cursor=next_cursor)

    async def get(self, document_id: UUID) -> DocumentView | None:
        """Fetch one of the caller's documents, or ``None`` if not visible (→ 404)."""
        document = await self._visible(document_id)
        if document is None:
            return None
        return await self._view(document)

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

        **Ownership** (tenant + owner), not the read predicate, is established
        *before* any write so a non-owner's document is never touched and is
        reported as 404 (INV-1/INV-2), not 403. Management stays an ownership
        decision on purpose: a grantee must not be able to delete the owner's
        document, and the connecting admin keeps the ability to remove a
        mirrored document their ACL mirror does not let them read.

        On success the row is deleted (the ORM ``delete-orphan`` cascade removes
        its chunks) **and** the stored object is removed via the #22 adapter,
        then ``document.deleted`` is audited (INV-6). Returns ``False`` when the
        document is not the caller's.

        Order: delete the row first, ``flush`` it, then remove the object **only
        if no other document (this tenant) still references the same storage
        key** (#269). Connector/legacy content-addressed objects can be shared;
        direct multipart keys are unique. The flush is required because the app
        sessionmaker runs ``autoflush=False`` (``db/session.py``); without it the
        ``count_by_storage_key`` read would still see the just-deleted row. All of
        this commits within this request's transaction at the router.
        """
        document = await self._owned(document_id)
        if document is None:
            return False
        deleted = await self._documents.delete(document_id)
        if not deleted:  # pragma: no cover — visibility already established
            return False
        await self._uploads.delete_for_document(document.id)
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
