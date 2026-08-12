"""Collections use-cases — create / list / get / update / delete (#46).

The orchestration layer for the ``/collections`` surface (ADR-0004: ``services/``
compose adapters; routers call exactly one service). It pairs the tenant-scoped
``db/`` ``CollectionRepository`` (the only SQL) with the one audit sink
(``collection.created`` / ``collection.deleted``, spec 0004 §2.4), and turns the
repository's storage-faithful :class:`~app.domain.entities.Collection` into the
wire projection the contract requires (``document_count`` is computed here, never
stored).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repository) *and* the
caller's ownership (this service). A collection in another tenant — or owned by
another user in the same tenant — is treated as **non-existent**: the read/update
returns ``None`` and the router maps that to **404** (existence non-disclosure;
never 403). The ``owner_id`` and ``tenant_id`` of a created collection come from
the resolved principal, never from request input.

Cursor pagination is keyset over ``(created_at, id)`` descending: the opaque
cursor encodes the **id** of the last item of the previous page; the service
decodes it, asks the repository for one more than the page size to detect "is
there a next page", and re-encodes the new boundary id. The repository resolves
the boundary's ``created_at`` in-database (no timestamp crosses the wire), so the
cursor stays small and dialect-independent. A malformed cursor is rejected
fail-closed (422).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.db.repositories import CollectionRepository, DocumentRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Collection
from app.services.audit import AuditSink
from app.storage import ObjectStore

log = get_logger(__name__)

# Pagination bounds mirror the contract's Limit parameter (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class CollectionView:
    """A collection projected for the wire (contract ``Collection`` schema).

    The storage-faithful :class:`~app.domain.entities.Collection` plus the
    computed ``document_count``. The router serialises this into the response
    model; the service never imports a Pydantic/HTTP type.
    """

    collection: Collection
    document_count: int


@dataclass(frozen=True, slots=True)
class CollectionPage:
    """One page of collections plus the opaque cursor for the next page."""

    items: list[CollectionView]
    next_cursor: str | None


# --- Cursor codec (opaque; carries the boundary row id) ---------------------

# A short, stable prefix so a decoded payload is recognisably one of ours and an
# arbitrary base64 string that happens to decode to a uuid-shaped value is still
# rejected unless it was minted here.
_CURSOR_PREFIX = "col:"


def _encode_cursor(collection_id: UUID) -> str:
    """Encode a boundary collection id as an opaque URL-safe cursor.

    The wire treats the cursor as opaque (contract ``Cursor`` parameter); the
    encoding here is an implementation detail. Only the id travels — the
    repository resolves the boundary's ``created_at`` in-database.
    """
    raw = f"{_CURSOR_PREFIX}{collection_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    """Decode an opaque cursor back into the boundary collection id.

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


class CollectionsService:
    """Create / list / get / update / delete collections for one principal.

    Constructed per-request with the session, the resolved ``tenant_id`` and
    ``owner_id`` (both from the token — never request input), and the audit sink
    + correlation context the router supplies. All ownership/tenancy enforcement
    lives here; the router only (de)serialises and maps ``None`` → 404.
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
        self._tenant_id = tenant_id
        self._repo = CollectionRepository(session, tenant_id)
        self._documents = DocumentRepository(session, tenant_id)
        self._owner_id = owner_id
        self._object_store = object_store
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    # --- internal helpers ---------------------------------------------------

    async def _view(self, collection: Collection) -> CollectionView:
        count = await self._repo.count_documents(collection.id)
        return CollectionView(collection=collection, document_count=count)

    def _owns(self, collection: Collection) -> bool:
        """Deny-by-default ownership check (spec 0004 §2.2, INV-2)."""
        return collection.owner_id == self._owner_id

    # --- use-cases ----------------------------------------------------------

    async def create(self, *, name: str, description: str | None) -> CollectionView:
        """Create a collection owned by the caller and audit it.

        ``tenant_id`` and ``owner_id`` are bound from the principal, never from
        the request body (spec 0004 §2.3). Emits ``collection.created`` through
        the one audit sink (INV-6); the audit row flushes within this request's
        transaction so it commits atomically with the collection.
        """
        collection = await self._repo.create(
            owner_id=self._owner_id, name=name, description=description
        )
        await self._audit.emit(
            action=AuditAction.COLLECTION_CREATED,
            actor=AuditActor.user(self._owner_id),
            resource_type="collection",
            resource_id=str(collection.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"name": collection.name},
        )
        # A freshly created collection holds no documents yet.
        return CollectionView(collection=collection, document_count=0)

    async def list_page(self, *, cursor: str | None, limit: int | None) -> CollectionPage:
        """Return one keyset page of the caller's own collections.

        Owner- and tenant-scoped (deny by default). Fetches ``limit + 1`` rows to
        decide whether a next page exists without a second round-trip; the extra
        row (if present) determines ``next_cursor`` and is then dropped.
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._repo.list_for_owner_page(
            self._owner_id, limit=page_size + 1, after_id=after_id
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        # One grouped COUNT for the whole page (#526) — see the same change in
        # ``document_service``. An empty collection is absent from the mapping
        # and defaults to 0, exactly as the single-id count returns.
        document_counts = await self._repo.count_documents_for([c.id for c in page])
        items = [
            CollectionView(collection=c, document_count=document_counts.get(c.id, 0)) for c in page
        ]
        return CollectionPage(items=items, next_cursor=next_cursor)

    async def get(self, collection_id: UUID) -> CollectionView | None:
        """Fetch one of the caller's collections, or ``None`` if not visible.

        Returns ``None`` for a missing id, a foreign-tenant id (the repository
        sees no row), *or* a same-tenant collection owned by another user
        (ownership check) — the router maps all three to 404 (INV-1/INV-2).
        """
        collection = await self._repo.get(collection_id)
        if collection is None or not self._owns(collection):
            return None
        return await self._view(collection)

    async def update(
        self,
        collection_id: UUID,
        *,
        name: str | None,
        description: str | None,
        set_description: bool,
    ) -> CollectionView | None:
        """Apply a partial update to one of the caller's collections.

        Visibility (tenant + ownership) is established *before* any write so a
        non-owner's collection is never mutated and is reported as 404 (INV-2),
        not 403. ``set_description`` distinguishes "description omitted" (leave
        as-is) from "description present" (write it, possibly clearing it).
        """
        existing = await self._repo.get(collection_id)
        if existing is None or not self._owns(existing):
            return None
        updated = await self._repo.update(
            collection_id,
            name=name,
            description=description,
            set_description=set_description,
        )
        if updated is None:  # pragma: no cover — visibility already established
            return None
        return await self._view(updated)

    async def delete(self, collection_id: UUID) -> bool:
        """Delete one of the caller's collections (cascades to docs + chunks).

        Returns ``False`` when the collection is not visible to the caller
        (missing / foreign tenant / other owner) so the router emits 404
        (INV-1/INV-2). On success the ORM ``delete-orphan`` cascade removes the
        collection's documents and *their* chunks in one transaction.

        Audit (spec 0004 §2.4, INV-6): the collection itself emits exactly one
        ``collection.deleted`` event, including when empty; each cascaded
        document also emits ``document.deleted``. All rows contain ids/counts
        only and flush within this request's transaction, committing atomically
        with the delete. A failed repository delete returns before the allowed
        collection event, so it can never fabricate success.

        The backing **object-store** bytes of the cascaded documents ARE removed
        here (#269) — the row+chunk cascade clears retrievable content, but the
        content-addressed MinIO objects would otherwise be orphaned. Cleanup runs
        after a ``flush`` and is guarded by ``count_by_storage_key``, so a
        content-addressed object is deleted only once no surviving document (this
        tenant) still references it; it is best-effort (a storage blip never fails
        the delete). Mirrors ``DocumentService.delete``.
        """
        existing = await self._repo.get(collection_id)
        if existing is None or not self._owns(existing):
            return False
        # Enumerate the documents the cascade will remove *before* deleting, so
        # each can be individually audited (spec 0004 §2.4 ``document.deleted``).
        documents = await self._documents.list_in_collection(collection_id)
        deleted = await self._repo.delete(collection_id)
        if not deleted:  # pragma: no cover — visibility already established
            return False
        await self._audit.emit(
            action=AuditAction.COLLECTION_DELETED,
            actor=AuditActor.user(self._owner_id),
            resource_type="collection",
            resource_id=str(collection_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"document_count": len(documents)},
        )
        for doc in documents:
            await self._audit.emit(
                action=AuditAction.DOCUMENT_DELETED,
                actor=AuditActor.user(self._owner_id),
                resource_type="document",
                resource_id=str(doc.id),
                outcome=AuditOutcome.ALLOWED,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={"collection_id": str(collection_id), "filename": doc.filename},
            )
            # Clear each cascaded document's chunk docs from the search index
            # (ADR-0010 §5) — after-commit, so a rollback never leaves the index
            # ahead of Postgres. Best-effort; the reindex command repairs gaps.
            self._enqueue_index_sync_after_commit(doc.id)

        await self._session.flush()
        # Remove the backing objects of the cascaded documents — the row+chunk
        # cascade above clears retrievable content but leaves the content-addressed
        # MinIO objects orphaned otherwise (#269). Best-effort + post-flush: a
        # storage blip must not fail the delete, and only an object no surviving
        # document (this tenant) still references is removed (shared-content guard).
        for storage_key in {d.storage_key for d in documents}:
            if await self._documents.count_by_storage_key(storage_key) == 0:
                try:
                    await self._object_store.delete(str(self._tenant_id), storage_key)
                except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
                    log.warning(
                        "collection_delete.object_cleanup_failed",
                        collection_id=str(collection_id),
                        storage_key=storage_key,
                        error=type(exc).__name__,
                    )
        return True

    def _enqueue_index_sync_after_commit(self, document_id: UUID) -> None:
        """Schedule a search-index sync to fire after the request commits.

        Mirrors ``DocumentService._enqueue_index_sync_after_commit``: a one-shot
        SQLAlchemy ``after_commit`` listener per cascaded document, going through
        ``tasks.enqueue_index_sync`` — the single enqueue point (ADR-0004),
        resolved at call time so a test can monkeypatch it.
        """
        from sqlalchemy import event

        import app.tasks as tasks

        tenant_id = self._tenant_id

        def _on_commit(_session: object) -> None:
            tasks.enqueue_index_sync(tenant_id, document_id)

        event.listen(self._session.sync_session, "after_commit", _on_commit, once=True)
