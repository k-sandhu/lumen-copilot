"""Connector sync Celery task — fetch a source → ingest its docs (#20, ADR-0009 §5).

Drives a connected source through ``syncing → ready|error`` off the request path
(backend/AGENTS.md: slow/burst work is a Celery task, never in-request). It
composes the connector framework with the **existing ingestion pipeline** (#21,
``app.tasks.ingest``) — no parse/chunk/embed/index logic is duplicated here:

1. **Resolve** the source (tenant-scoped) and its connector (auto-discovered).
2. **Mark** the source ``syncing``.
3. **Fetch** via the connector (the web connector's SSRF-guarded fetch), yielding
   :class:`~app.connectors.base.FetchedDoc` passages of readable text.
4. **Reconcile** idempotently: delete the documents the previous sync produced for
   this source (the FK/ORM cascade removes their chunks), so a re-sync **replaces**
   rather than duplicates (mirrors ingestion's chunk-replace idempotency).
5. **Ingest** each fetched doc by reusing the pipeline: store the extracted text
   as a ``text/plain`` object (#22), create a ``Document`` linked to ``source_id``
   in the source's backing collection, then run
   :func:`app.tasks.ingest.ingest_document_async` (parse → chunk → embed →
   pgvector), tenant/owner-scoped.
6. **Advance** the source: ``ready`` with ``indexed_count`` + ``last_synced_at`` on
   success, or ``error`` with ``last_error`` on a fetch/SSRF fault — never a silent
   drop.

Tenant- and owner-scoped throughout (INV-1/INV-2): the source, its collection, its
documents, and their chunks are all read/written under the source's ``tenant_id``
and ``owner_id``. The connector re-runs the full SSRF guard on every fetch hop
(ADR-0009 §3) — the request-path pre-check is not trusted here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, FetchedDoc
from app.connectors.registry import UnknownConnectorError, get_connector
from app.core.config import Settings, get_settings
from app.db import models
from app.db.repositories import DocumentRepository, SourceRepository
from app.db.session import session_scope
from app.domain.entities import DocumentStatus, SourceStatus, WebSourceMode
from app.llm import LLMGateway
from app.storage import ObjectStore
from app.tasks.celery_app import celery_app
from app.tasks.ingest import ingest_document_async
from app.tasks.rate_limit import RateLimiter, RedisFixedWindowRateLimiter

# A fetched web document is stored as plain text for the ingestion pipeline (its
# parser handles text/plain natively, no library needed).
_TEXT_MIME = "text/plain"


@dataclass(frozen=True, slots=True)
class SyncResult:
    """The outcome of one source sync — what the task returns / tests assert."""

    source_id: UUID
    status: SourceStatus
    indexed_count: int
    error: str | None = None


def _safe_filename(title: str, index: int) -> str:
    """A stable, filesystem-safe filename for a fetched doc (titles vary wildly)."""
    base = "".join(c if c.isalnum() or c in {"-", "_", " "} else "_" for c in title).strip()
    base = base[:120] or "page"
    return f"{index:04d}-{base}.txt"


async def sync_source_async(
    tenant_id: UUID,
    source_id: UUID,
    *,
    settings: Settings,
    object_store: ObjectStore,
    gateway: LLMGateway,
) -> SyncResult:
    """Run one full source sync (the async core; the Celery task wraps this).

    Pure orchestration over injected adapters (connector / storage / model / db)
    so it is directly unit-testable with fakes offline. Tenant/owner-scoped
    throughout (INV-1/INV-2).

    A fetch/SSRF/connector fault marks the source ``error`` with the reason and
    returns (no raise) — a recorded terminal state, never a crash. The status
    machine: claim → ``syncing`` (committed first so a crash mid-run is visible),
    then ``ready`` (with counts) or ``error`` (with reason).
    """
    log = structlog.get_logger(__name__)

    # --- Phase 1: resolve the source + connector, claim it as `syncing`. -----
    async with session_scope() as session:
        sources = SourceRepository(session, tenant_id)
        source = await sources.get(source_id)
        if source is None:
            # Deleted (or never existed in this tenant) — idempotent no-op.
            return SyncResult(source_id, SourceStatus.ERROR, 0, "source not found")
        owner_id = source.owner_id
        source_type = source.type
        config = dict(source.config)
        await sources.update_status(source_id, status=SourceStatus.SYNCING)

    try:
        connector = get_connector(source_type)
    except UnknownConnectorError:
        return await _fail(tenant_id, source_id, f"unknown connector type {source_type!r}")

    collection_id = _resolve_collection_id(config)
    if collection_id is None:
        return await _fail(tenant_id, source_id, "source has no backing collection")

    # --- Phase 2: fetch via the connector (re-runs the full SSRF guard). ------
    # ``source`` is a frozen, detached domain entity — safe to hand the connector
    # after the session that read it has closed.
    try:
        fetched = list(await connector.sync(source))
    except ConnectorError as exc:
        # Includes UrlBlockedError (SSRF) — a permanent rejection of this source.
        return await _fail(tenant_id, source_id, f"fetch failed: {exc.detail}")

    # --- Phase 3: reconcile (replace prior docs) then ingest each fetched doc. -
    detected_mode = _detect_mode(source_type, fetched, config)
    indexed = 0
    async with session_scope() as session:
        documents = DocumentRepository(session, tenant_id)
        prior = await documents.list_for_source(source_id)
        for stale in prior:
            await documents.delete(stale.id)

    for index, fetched_doc in enumerate(fetched):
        try:
            await _ingest_one(
                tenant_id,
                source_id=source_id,
                owner_id=owner_id,
                collection_id=collection_id,
                fetched=fetched_doc,
                index=index,
                settings=settings,
                object_store=object_store,
                gateway=gateway,
            )
        except Exception as exc:  # noqa: BLE001 — one bad doc must not fail the sync
            log.warning(
                "source_sync.doc_failed",
                source_id=str(source_id),
                url=fetched_doc.url,
                error=type(exc).__name__,
            )
            continue
        indexed += 1

    # --- Phase 4: advance the source to ready. -------------------------------
    async with session_scope() as session:
        await SourceRepository(session, tenant_id).update_status(
            source_id,
            status=SourceStatus.READY,
            indexed_count=indexed,
            last_error=None,
            set_last_error=True,
            last_synced_at=datetime.now(UTC),
            set_last_synced_at=True,
        )
        # Persist the detected mode so the grid shows page/feed/sitemap.
        await _record_mode(session, tenant_id, source_id, detected_mode)

    return SyncResult(source_id, SourceStatus.READY, indexed)


async def _ingest_one(
    tenant_id: UUID,
    *,
    source_id: UUID,
    owner_id: UUID,
    collection_id: UUID,
    fetched: FetchedDoc,
    index: int,
    settings: Settings,
    object_store: ObjectStore,
    gateway: LLMGateway,
) -> None:
    """Store one fetched doc + register a Document + run the ingestion pipeline.

    Reuses the existing pipeline end-to-end: the text is stored as a ``text/plain``
    object (#22), a ``Document`` linked to ``source_id`` is created ``pending``,
    and :func:`ingest_document_async` parses → chunks → embeds → indexes it. All
    tenant/owner-scoped.
    """
    data = fetched.text.encode("utf-8")
    stored = await object_store.put(
        tenant_id=str(tenant_id),
        data=data,
        content_type=_TEXT_MIME,
        filename=_safe_filename(fetched.title, index),
    )
    async with session_scope() as session:
        document = await DocumentRepository(session, tenant_id).create(
            owner_id=owner_id,
            collection_id=collection_id,
            filename=_safe_filename(fetched.title, index),
            mime_type=_TEXT_MIME,
            size_bytes=stored.size_bytes,
            storage_key=stored.key,
            status=DocumentStatus.PENDING,
            source_id=source_id,
        )
        document_id = document.id

    # Reuse the ingestion pipeline (parse → chunk → embed → persist) verbatim.
    await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=object_store,
        gateway=gateway,
    )


async def _fail(tenant_id: UUID, source_id: UUID, reason: str) -> SyncResult:
    """Mark a source ``error`` with ``reason`` (own transaction). ADR-0009 §4."""
    async with session_scope() as session:
        await SourceRepository(session, tenant_id).update_status(
            source_id,
            status=SourceStatus.ERROR,
            last_error=reason,
            set_last_error=True,
        )
    return SyncResult(source_id, SourceStatus.ERROR, 0, reason)


def _resolve_collection_id(config: dict[str, object]) -> UUID | None:
    """The backing collection id stored on the source config (or ``None``)."""
    raw = config.get("collection_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _detect_mode(
    source_type: str, fetched: list[FetchedDoc], config: dict[str, object]
) -> WebSourceMode | None:
    """Refine the source ``mode`` from the sync result (ADR-0009 §2, contract).

    The creation-time heuristic (``mode_from_url``) gave the source a non-null
    ``mode`` before any fetch; this **refines** it from the actual fan-out:

    * a single fetched doc ⇒ ``page``;
    * many docs ⇒ a multi-document fan-out, i.e. ``feed`` or ``sitemap``. We keep
      the creation-time guess when it already said ``sitemap`` (a ``.xml`` /
      sitemap URL), otherwise label it ``feed``.

    Non-web connectors record no mode (``None``). The connector's content-based
    :func:`app.connectors.web.connector.detect_mode` is exercised in its own
    tests; here we only need the coarse grid label without a second fetch.
    """
    if source_type != "web":
        return None
    if len(fetched) <= 1:
        return WebSourceMode.PAGE
    prior = config.get("mode")
    if prior == WebSourceMode.SITEMAP.value:
        return WebSourceMode.SITEMAP
    return WebSourceMode.FEED


async def _record_mode(
    session: AsyncSession, tenant_id: UUID, source_id: UUID, mode: WebSourceMode | None
) -> None:
    """Persist the detected ``mode`` onto the source config (best-effort).

    Updates the ``config`` JSON in place on the same row the caller just set to
    ``ready`` (one transaction). Tenant-scoped (INV-1); a no-op for ``None`` mode
    or a missing row.
    """
    if mode is None:
        return
    stmt = select(models.Source).where(
        models.Source.tenant_id == tenant_id, models.Source.id == source_id
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:  # pragma: no cover — just updated above
        return
    new_config = dict(row.config)
    new_config["mode"] = mode.value
    row.config = new_config
    await session.flush()


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.sync_source",
    bind=True,
    acks_late=True,
    max_retries=None,
)
def sync_source(self: object, tenant_id: str, source_id: str) -> dict[str, object]:
    """Celery entrypoint: sync one connected source (the sync wrapper).

    Resolves config + adapters, runs :func:`sync_source_async` on a fresh event
    loop, and returns the result dict. A fetch/SSRF fault is already recorded as
    ``error`` on the source by the async core (returned, not raised), so the task
    does not retry a permanently-blocked URL. Args are strings (Celery serializes
    JSON, not UUIDs); parsed back to ``UUID`` here.
    """
    settings = get_settings()
    tid = UUID(tenant_id)
    sid = UUID(source_id)
    object_store = ObjectStore(settings)
    gateway = LLMGateway(settings)

    result = asyncio.run(
        sync_source_async(
            tid,
            sid,
            settings=settings,
            object_store=object_store,
            gateway=gateway,
        )
    )
    return _as_dict(result)


def _as_dict(result: SyncResult) -> dict[str, object]:
    """Render a :class:`SyncResult` as the JSON-able task return value."""
    return {
        "source_id": str(result.source_id),
        "status": result.status.value,
        "indexed_count": result.indexed_count,
        "error": result.error,
    }


def _build_rate_limiter(settings: Settings) -> RateLimiter:
    """Construct the Redis-backed per-tenant fetch rate limiter from settings.

    Factory seam so a test can substitute a deterministic fake (no Redis).
    """
    return RedisFixedWindowRateLimiter(
        settings.redis_url,
        max_per_window=settings.source_sync_rate_max_per_window,
        window_seconds=settings.source_sync_rate_window_seconds,
    )


def enqueue_source_sync(
    tenant_id: UUID,
    source_id: UUID,
    *,
    rate_limiter: RateLimiter | None = None,
) -> None:
    """Enqueue a sync for a source (the seam the sources service calls after commit).

    The single enqueue point for source syncs (ADR-0004: tasks are enqueued only
    from ``tasks/``). Mirrors ``enqueue_ingestion``: best-effort + bounded against
    the broker so a transient broker outage neither turns the request into a 500
    nor blocks it — an unreachable broker raises ``kombu.OperationalError`` in
    seconds, which is logged and swallowed (the source stays ``pending``; a
    re-drive is out of scope). Ids are passed as strings (Celery's JSON serializer).

    **Per-tenant fetch rate limit (ADR-0009 §3, load-bearing).** Before the
    message is published the tenant's fixed-window fetch budget is checked
    (Redis-backed). When the window is exhausted the sync is **deferred** — the
    task is re-enqueued with a ``countdown`` backoff rather than published
    immediately — so a single tenant cannot make the worker fan out unbounded
    outbound fetches, and the frozen ``/sources`` contract needs no 429 (the HTTP
    response already returned ``pending``/``syncing``; the deferral is invisible
    to the wire). ``rate_limiter`` is injectable for tests; production builds the
    Redis limiter from settings.
    """
    from kombu.exceptions import OperationalError

    log = structlog.get_logger(__name__)

    limiter = rate_limiter if rate_limiter is not None else _build_rate_limiter(get_settings())
    countdown = 0
    if not limiter.try_acquire(tenant_id):
        # Window exhausted — defer (re-enqueue with backoff), never drop and never
        # surface an HTTP error (ADR-0009 §3; the /sources contract is frozen).
        countdown = get_settings().source_sync_rate_backoff_seconds
        log.info(
            "source_sync.rate_limited_deferred",
            source_id=str(source_id),
            tenant_id=str(tenant_id),
            countdown_seconds=countdown,
        )

    try:
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
            sync_source.apply_async(
                args=(str(tenant_id), str(source_id)),
                connection=connection,
                retry=False,
                countdown=countdown,
            )
    except OperationalError as exc:
        log.warning(
            "source_sync.enqueue_failed",
            source_id=str(source_id),
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )


__all__ = [
    "SyncResult",
    "enqueue_source_sync",
    "sync_source",
    "sync_source_async",
]
