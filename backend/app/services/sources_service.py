"""Sources use-cases — the ``/sources`` surface (issue #20, ADR-0009 §5).

The orchestration layer for the connector framework's first surface (ADR-0004:
``services/`` compose adapters; routers call exactly one service). It pairs:

* the tenant-scoped ``db/`` ``SourceRepository`` + ``CollectionRepository`` +
  ``DocumentRepository`` (the only SQL) — sources, their backing collection, and
  the documents a sync ingests;
* the auto-discovered **connector registry** (``app.connectors``) — the ``web``
  connector validates + SSRF-checks the URL *before* a row is written;
* the one ``AuditSink`` (spec 0004 §2.4) — ``source.added`` / ``synced`` /
  ``deleted`` (ADR-0009 §5).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repositories) *and* the
caller's ownership (this service). A source in another tenant — or owned by
another user in the same tenant — is treated as **non-existent**: the read/op
returns ``None``/``False`` and the router maps that to **404** (existence
non-disclosure; never 403). The ``owner_id``/``tenant_id`` of a created source
come from the resolved principal, never from request input.

**SSRF (ADR-0009 §3, load-bearing).** ``add`` calls the connector's
``validate_config``, which runs the scheme/host SSRF pre-check; a rejection
raises :class:`~app.connectors.base.ConnectorConfigError` (``url_blocked``) which
the router maps to **422**. The full per-hop guard runs again in the sync task at
fetch time.

**Sync is never in the request path.** ``add`` and ``resync`` enqueue the Celery
sync task (``app.tasks.enqueue_source_sync`` — the single enqueue point, ADR-0004)
after the request transaction commits; the worker drives the source through
``pending|syncing → ready|error``.

Cursor pagination mirrors ``document_service``: a keyset over ``(created_at, id)``
descending, the opaque cursor carrying only the boundary id; a malformed cursor
is rejected fail-closed (422).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorConfigError
from app.connectors.registry import UnknownConnectorError, get_connector
from app.core.errors import ValidationError
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    SourceRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Source, SourceStatus
from app.services.audit import AuditSink

# Pagination bounds mirror the contract's Limit parameter (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# A short, stable cursor prefix so a decoded payload is recognisably one of ours.
_CURSOR_PREFIX = "src:"


@dataclass(frozen=True, slots=True)
class SourcePage:
    """One page of sources plus the opaque cursor for the next page."""

    items: list[Source]
    next_cursor: str | None


def _encode_cursor(source_id: UUID) -> str:
    """Encode a boundary source id as an opaque URL-safe cursor."""
    raw = f"{_CURSOR_PREFIX}{source_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    """Decode an opaque cursor back into the boundary source id (fail-closed 422)."""
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


class SourcesService:
    """Add / list / sync / delete connected sources for one principal.

    Constructed per-request with the session, the resolved ``tenant_id`` and
    ``owner_id`` (both from the token — never request input), and the audit sink
    + correlation context the router supplies. All ownership/tenancy enforcement
    lives here; the router only (de)serialises and maps ``None``/``False`` → 404
    (and ``ConnectorConfigError`` → 422).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        audit: AuditSink,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._session = session
        self._sources = SourceRepository(session, tenant_id)
        self._collections = CollectionRepository(session, tenant_id)
        self._documents = DocumentRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._audit = audit
        self._request_id = request_id
        self._source_ip = source_ip

    # --- internal helpers ---------------------------------------------------

    def _owns(self, source: Source) -> bool:
        """Deny-by-default ownership check (spec 0004 §2.2, INV-2)."""
        return source.owner_id == self._owner_id

    async def _visible(self, source_id: UUID) -> Source | None:
        """Fetch a source the caller may see, or ``None`` (→ 404).

        ``None`` for a missing id, a foreign-tenant id (the repository sees no
        row), *or* a same-tenant source owned by another user (ownership check) —
        INV-1/INV-2 collapse all three to 404 at the router.
        """
        source = await self._sources.get(source_id)
        if source is None or not self._owns(source):
            return None
        return source

    # --- use-cases ----------------------------------------------------------

    async def add(self, *, source_type: str, url: str) -> Source:
        """Add a source: validate + SSRF-check, create the row, enqueue first sync.

        Order (fail-closed):

        1. **Connector** — resolve the connector for ``source_type`` (unknown type
           → 422 ``unsupported_source_type``).
        2. **Validate + SSRF** — ``connector.validate_config`` runs the scheme/host
           SSRF pre-check; a blocked URL raises ``ConnectorConfigError``
           (``url_blocked`` → 422) *before* anything is written (ADR-0009 §3).
        3. **Backing collection** — create a collection owned by the caller to home
           the source's ingested documents (``documents.collection_id`` is
           non-null); its id is stored on the source config so the sync task can
           find it.
        4. **Source row** — ``status=pending``, owned by the caller.
        5. **Audit** ``source.added`` (INV-6).
        6. **Enqueue** the first sync after commit (never in the request path).

        Raises:
            ValidationError: unknown ``source_type``, or an invalid / SSRF-blocked
                URL — all mapped to **422** (INV-8). The connector's stable ``code``
                (``url_blocked`` for SSRF) is carried onto the Problem so the wire
                distinguishes the cases (ADR-0009 §3).
        """
        try:
            connector = get_connector(source_type)
        except UnknownConnectorError as exc:
            raise ValidationError(
                f"unsupported source type {source_type!r}",
                code="unsupported_source_type",
            ) from exc

        # SSRF + config validation. The connector raises a vendor-free
        # ConnectorConfigError (e.g. url_blocked); map it to the HTTP-aware
        # ValidationError (422) here so the router stays I/O- and error-mapping-free
        # and the connector boundary keeps emitting domain errors (ADR-0004).
        try:
            validated = connector.validate_config({"url": url})
        except ConnectorConfigError as exc:
            raise ValidationError(exc.detail, code=exc.code) from exc

        # A backing collection homes the source's ingested documents. Named after
        # the connector + URL so it is recognisable in the documents surface.
        collection = await self._collections.create(
            owner_id=self._owner_id,
            name=f"{source_type}: {validated.get('url', url)}",
            description="Documents ingested from a connected source (ADR-0009).",
        )
        config: dict[str, object] = dict(validated)
        config["collection_id"] = str(collection.id)

        source = await self._sources.create(
            owner_id=self._owner_id,
            type=source_type,
            config=config,
            status=SourceStatus.PENDING,
        )

        await self._audit.emit(
            action=AuditAction.SOURCE_ADDED,
            actor=AuditActor.user(self._owner_id),
            resource_type="source",
            resource_id=str(source.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"type": source_type, "url": str(config.get("url"))},
        )

        self._enqueue_sync_after_commit(source.id)
        return source

    async def list_page(self, *, cursor: str | None, limit: int | None) -> SourcePage:
        """Return one keyset page of the caller's own sources (newest first).

        Owner- and tenant-scoped (deny by default). Fetches ``limit + 1`` rows to
        decide whether a next page exists without a second round-trip; the extra
        row (if present) sets ``next_cursor`` and is then dropped.
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._sources.list_for_owner_page(
            self._owner_id, limit=page_size + 1, after_id=after_id
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return SourcePage(items=page, next_cursor=next_cursor)

    async def get(self, source_id: UUID) -> Source | None:
        """Fetch one of the caller's sources, or ``None`` if not visible (→ 404)."""
        return await self._visible(source_id)

    async def resync(self, source_id: UUID) -> tuple[Source, bool] | None:
        """Re-enqueue a sync for one of the caller's sources.

        Returns ``(source, enqueued)`` where ``enqueued`` is ``False`` when the
        source was already ``syncing`` (a no-op → the contract's 200) and ``True``
        when a fresh sync was enqueued and the status moved to ``syncing`` (→ 202).
        Returns ``None`` when the source is not visible to the caller (→ 404).
        Audits ``source.synced`` on a real enqueue (INV-6).
        """
        source = await self._visible(source_id)
        if source is None:
            return None
        if source.status == SourceStatus.SYNCING:
            # Already in flight — no new sync, no new audit (idempotent no-op).
            return source, False

        updated = await self._sources.update_status(source_id, status=SourceStatus.SYNCING)
        assert updated is not None  # visibility already established  # noqa: S101

        await self._audit.emit(
            action=AuditAction.SOURCE_SYNCED,
            actor=AuditActor.user(self._owner_id),
            resource_type="source",
            resource_id=str(source_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"type": source.type},
        )

        self._enqueue_sync_after_commit(source_id)
        return updated, True

    async def delete(self, source_id: UUID) -> bool:
        """Delete one of the caller's sources (cascades its documents, ADR-0009 §5).

        Visibility (tenant + ownership) is established before any write so a
        non-owner's source is never touched and is reported as 404 (INV-1/INV-2),
        not 403. On success the row is deleted; the FK ``ON DELETE CASCADE`` on
        ``documents.source_id`` removes the source's documents (and their chunks).
        Audits ``source.deleted`` (INV-6). Returns ``False`` when not visible.

        Note: the source's ingested **object-store** bytes are not swept here —
        the cascade removes the document rows and their chunks (the retrievable
        content); orphaned objects are content-addressed and tenant-scoped, and a
        storage sweep is a separate concern (out of this issue's scope).
        """
        source = await self._visible(source_id)
        if source is None:
            return False
        deleted = await self._sources.delete(source_id)
        if not deleted:  # pragma: no cover — visibility already established
            return False
        await self._audit.emit(
            action=AuditAction.SOURCE_DELETED,
            actor=AuditActor.user(self._owner_id),
            resource_type="source",
            resource_id=str(source_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"type": source.type},
        )
        return True

    # --- enqueue seam -------------------------------------------------------

    def _enqueue_sync_after_commit(self, source_id: UUID) -> None:
        """Schedule the sync enqueue to fire after the request commits.

        Mirrors ``DocumentService._enqueue_ingestion_after_commit``: a one-shot
        SQLAlchemy ``after_commit`` listener so the Celery message is sent only
        once the source row is durably committed (and never on rollback).
        Enqueueing goes through ``tasks.enqueue_source_sync`` — the single enqueue
        point (ADR-0004), resolved by name at call time so a test can monkeypatch
        it.
        """
        from sqlalchemy import event

        import app.tasks as tasks

        tenant_id = self._tenant_id

        def _on_commit(_session: object) -> None:
            tasks.enqueue_source_sync(tenant_id, source_id)

        event.listen(self._session.sync_session, "after_commit", _on_commit, once=True)


__all__ = ["SourcePage", "SourcesService"]
