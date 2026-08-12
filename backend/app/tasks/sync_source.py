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

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import (
    AclMappingContext,
    Connector,
    ConnectorError,
    ConnectorRun,
    CursorExpiredError,
    FetchedDoc,
    FullSyncResult,
    PageIntegrity,
    get_fetch_changes,
    get_map_acl,
    get_oauth_spec,
)
from app.connectors.oauth import (
    REAUTHORIZE_REQUIRED,
    OAuthExchangeError,
    OAuthGrantDeadError,
    build_authenticated_client,
    refresh_access_token,
)
from app.connectors.registry import UnknownConnectorError, get_connector
from app.core.config import Settings, get_settings
from app.core.errors import DependencyError, NotFoundError
from app.db import models
from app.db.repositories import (
    AuditEventRepository,
    DocumentRepository,
    SourceRepository,
    UserRepository,
)
from app.db.session import tenant_session_scope
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AuditOutcome,
    Document,
    DocumentStatus,
    Role,
    Source,
    SourceStatus,
    WebSourceMode,
)
from app.ingestion.contract import ensure_embedding_contract
from app.llm import LLMGateway
from app.services.audit import AuditSink
from app.services.secrets_service import SecretsService, build_secrets_service
from app.storage import ObjectStore
from app.tasks.celery_app import celery_app
from app.tasks.index_sync import (
    attest_index_acl_fresh_async,
    stamp_index_acl_stale_async,
    sync_document_index_async,
)
from app.tasks.ingest import IngestionResult, ingest_document_async
from app.tasks.rate_limit import RateLimiter, RedisFixedWindowRateLimiter
from app.tasks.runner import run_task

# A fetched web document is stored as plain text for the ingestion pipeline (its
# parser handles text/plain natively, no library needed).
_TEXT_MIME = "text/plain"

# Recorded in ``sources.last_error`` when an incremental replay ended on an
# unprovable (``integrity=incomplete``) page: every mirrored document of the
# source is stale-stamped (denied), the cursor was NOT advanced past that page,
# and the durable ``acl_resync_required`` state makes the next run a FULL
# resync — the only run that re-examines every document (ADR-0019 §3). The
# source is deliberately not ``ready`` while this stands, and stays not-ready
# until that full resync completes: health must never report success over a
# mirror that is still partly unrecovered.
ACL_MIRROR_INCOMPLETE = "acl_mirror_incomplete"

log = structlog.get_logger(__name__)


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
    async with tenant_session_scope(tenant_id) as session:
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
    # after the session that read it has closed. The framework builds the per-run
    # execution context (ADR-0019 §4): for an OAuth connector that means
    # resolving the vault-held refresh token and running the refresh grant HERE —
    # connector code never sees a raw token, only an authenticated client. A dead
    # grant fails the sync closed as *reauthorize required* (ADR-0019 §1),
    # audited as the sync-stage ``source.synced`` outcome=error. For a
    # ``map_acl``-declaring connector the framework also freezes the attested-
    # identity snapshot into the run (ADR-0019 §2 — sync-time snapshot).
    enforce_acl = get_map_acl(connector) is not None
    acl_context = await _build_acl_context(tenant_id) if enforce_acl else None
    try:
        run = await _build_run(
            connector, source, tenant_id=tenant_id, settings=settings, acl_context=acl_context
        )
    except OAuthGrantDeadError:
        return await _fail_reauthorize(tenant_id, source_id, source_type=source_type)
    except OAuthExchangeError as exc:
        return await _fail(tenant_id, source_id, f"token refresh failed: {exc.code}")

    baseline_cursor: str | None = None
    try:
        # --- Incremental path (ADR-0019 §3): capability + a stored cursor, and
        # no outstanding full-resync requirement. ``acl_resync_required`` means
        # a previous replay stale-stamped EVERY mirrored document of this
        # source; only re-examining the whole corpus can restore them, so an
        # incremental replay is skipped outright — it would re-examine the
        # subset its pages happen to report and leave the rest denied while
        # reporting success. This is the §3 escalation, and it is immediate
        # because until it completes the source's content is invisible.
        if (
            get_fetch_changes(connector) is not None
            and source.sync_cursor
            and not source.acl_resync_required
        ):
            try:
                return await _sync_incremental(
                    tenant_id,
                    source,
                    connector,
                    run,
                    settings=settings,
                    object_store=object_store,
                    gateway=gateway,
                    collection_id=collection_id,
                    enforce_acl=enforce_acl,
                )
            except CursorExpiredError:
                # HTTP 410: clear the cursor and fall through to a full resync
                # in this same run (ADR-0019 §3).
                log.info("source_sync.cursor_expired", source_id=str(source_id))
                async with tenant_session_scope(tenant_id) as session:
                    await SourceRepository(session, tenant_id).set_sync_cursor(source_id, None)
            except ConnectorError as exc:
                # Pages committed so far (mutations + cursor) stand — the next
                # run resumes from the last committed token (never a skip).
                return await _fail(tenant_id, source_id, f"fetch failed: {exc.detail}")

        try:
            sync_result = await connector.sync(source, run)
            fetched = list(sync_result)
            if isinstance(sync_result, FullSyncResult):
                baseline_cursor = sync_result.baseline_cursor
        except ConnectorError as exc:
            # Includes UrlBlockedError (SSRF) — a permanent rejection of this source.
            return await _fail(tenant_id, source_id, f"fetch failed: {exc.detail}")
    finally:
        await run.http.aclose()

    # --- Phase 3: reconcile (replace prior docs) then ingest each fetched doc. -
    detected_mode = _detect_mode(source_type, fetched, config)
    indexed = 0
    failed = 0
    orphaned_keys: list[str] = []
    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        prior = await documents.list_for_source(source_id)
        for stale in prior:
            await documents.delete(stale.id)
        # Flush the queued deletes so the count below sees the POST-delete state.
        # The app sessionmaker runs autoflush=False (db/session.py), and
        # DocumentRepository.delete does not flush, so without this explicit flush
        # count_by_storage_key would still see the just-deleted rows, find every
        # key still referenced, and skip all cleanup — i.e. never delete anything.
        await session.flush()
        # Collect the prior objects that NO surviving document (any source, this
        # tenant) still references, so they can be removed after this delete
        # commits. Objects are content-addressed, so a distinct key set dedupes
        # shared bytes.
        for storage_key in {stale.storage_key for stale in prior}:
            if await documents.count_by_storage_key(storage_key) == 0:
                orphaned_keys.append(storage_key)

    # Remove the orphaned objects AFTER the row-delete transaction commits — a
    # re-sync re-stores content under NEW content-addressed keys, so a bare row
    # delete would leak the prior bytes into the bucket without bound (#269).
    # Best-effort and post-commit (mirrors the index cleanup below): a storage
    # blip must not fail an otherwise-good re-sync, and a stray object is a
    # benign leak, never a row pointing at missing bytes.
    for storage_key in orphaned_keys:
        try:
            await object_store.delete(str(tenant_id), storage_key)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            log.warning(
                "source_sync.object_cleanup_failed",
                source_id=str(source_id),
                storage_key=storage_key,
                error=type(exc).__name__,
            )

    # Clear the replaced documents' chunk docs from the search index (ADR-0010
    # §5) — after the reconcile transaction committed, so the sync reads the
    # deleted state. Best-effort: the index is derived and repairable (`python
    # -m app.search.reindex`), and slice 3's hydration re-check drops any stale
    # hit anyway, so an engine outage must not fail an otherwise-good re-sync.
    for stale in prior:
        try:
            await sync_document_index_async(tenant_id, stale.id, settings=settings)
        except DependencyError as exc:
            log.warning(
                "source_sync.index_cleanup_failed",
                source_id=str(source_id),
                document_id=str(stale.id),
                error=exc.code,
            )

    for index, fetched_doc in enumerate(fetched):
        try:
            outcome = await _ingest_one(
                tenant_id,
                source_id=source_id,
                owner_id=owner_id,
                collection_id=collection_id,
                fetched=fetched_doc,
                index=index,
                acl_enforced=enforce_acl,
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
            failed += 1
            continue
        if outcome.status is DocumentStatus.READY:
            indexed += 1
        else:
            failed += 1
            log.warning(
                "source_sync.doc_terminal_failure",
                source_id=str(source_id),
                document_id=str(outcome.document_id),
                status=outcome.status.value,
            )

    if failed:
        return await _fail(
            tenant_id,
            source_id,
            f"{failed} of {len(fetched)} fetched document(s) failed to ingest",
            indexed_count=indexed,
        )

    # --- Phase 4: advance the source to ready. -------------------------------
    now = datetime.now(UTC)
    async with tenant_session_scope(tenant_id) as session:
        sources = SourceRepository(session, tenant_id)
        await sources.update_status(
            source_id,
            status=SourceStatus.READY,
            indexed_count=indexed,
            last_error=None,
            set_last_error=True,
            last_synced_at=now,
            set_last_synced_at=True,
        )
        # Persist the detected mode so the grid shows page/feed/sitemap.
        await _record_mode(session, tenant_id, source_id, detected_mode)
        if source.acl_resync_required:
            # THE ONLY place the full-resync requirement is cleared (ADR-0019
            # §3). A full sync re-enumerated the source and re-examined every
            # surviving document, so the rows an incomplete replay stamped
            # stale have all been restored or removed — the requirement is
            # satisfied by construction, not by assumption. This runs in the
            # same transaction that publishes READY + fresh health.
            await sources.record_acl_resync_required(source_id, False)
        if baseline_cursor is not None:
            # The pre-enumeration change-log baseline (ADR-0019 §3): the next
            # sync replays incrementally from here, covering the enumeration
            # window.
            await sources.set_sync_cursor(source_id, baseline_cursor)
        if enforce_acl:
            unmapped = await _count_unmapped(session, tenant_id, source_id)
            await sources.record_acl_health(
                source_id, acl_synced_at=now, unmapped_acl_count=unmapped
            )

    return SyncResult(source_id, SourceStatus.READY, indexed)


async def _ingest_one(
    tenant_id: UUID,
    *,
    source_id: UUID,
    owner_id: UUID,
    collection_id: UUID,
    fetched: FetchedDoc,
    index: int,
    acl_enforced: bool,
    settings: Settings,
    object_store: ObjectStore,
    gateway: LLMGateway,
) -> IngestionResult:
    """Store one fetched doc + register a Document + run the ingestion pipeline.

    Reuses the existing pipeline end-to-end: the payload is stored as an object
    (#22 — extracted text as ``text/plain``, or the connector's binary
    pass-through under its own MIME type), a ``Document`` linked to
    ``source_id`` is created ``pending``, and :func:`ingest_document_async`
    parses → chunks → embeds → indexes it. All tenant/owner-scoped.

    ``acl_enforced`` is the **mandatory write-mode argument** (ADR-0019 §2):
    derived structurally by the caller from the connector's ``map_acl``
    capability, never defaulted — a managed-source document written
    ``acl_enforced=False`` is a defect. The mirrored ACL (principals + scope
    chain) rides ``fetched.acl``; its ``acl_synced_at`` advances only because
    this sync examined it (§2 refresh cadence).
    """
    payload = fetched.data if fetched.data is not None else fetched.text.encode("utf-8")
    mime_type = fetched.mime_type if fetched.data is not None else _TEXT_MIME
    stored = await object_store.put(
        tenant_id=str(tenant_id),
        data=payload,
        content_type=mime_type,
        filename=_safe_filename(fetched.title, index),
    )
    acl = fetched.acl
    async with tenant_session_scope(tenant_id) as session:
        document = await DocumentRepository(session, tenant_id).create(
            owner_id=owner_id,
            collection_id=collection_id,
            filename=_safe_filename(fetched.title, index),
            mime_type=mime_type,
            size_bytes=stored.size_bytes,
            storage_key=stored.key,
            acl_enforced=acl_enforced,
            status=DocumentStatus.PENDING,
            source_id=source_id,
            external_id=fetched.external_id,
            acl_principals=sorted(acl.principals) if acl is not None else None,
            acl_synced_at=datetime.now(UTC) if acl is not None else None,
            acl_scope_ids=sorted(acl.scope_ids) if acl is not None else None,
        )
        document_id = document.id

    # Reuse the ingestion pipeline (parse → chunk → embed → persist) verbatim.
    return await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=object_store,
        gateway=gateway,
    )


async def _count_unmapped(session: AsyncSession, tenant_id: UUID, source_id: UUID) -> int:
    """Documents whose mirrored ACL admits no one (ADR-0019 §2 health count).

    Counted from the source of truth after the run so full and incremental
    syncs report identically: an ``acl_enforced`` document with an **empty**
    mirrored set is ingested but invisible (fail closed) — an admin sees the
    silent-deny volume, and attestation lights these up on a later sync.
    """
    documents = await DocumentRepository(session, tenant_id).list_for_source(source_id)
    return sum(1 for d in documents if d.acl_enforced and not d.acl_principals)


async def _build_acl_context(tenant_id: UUID) -> AclMappingContext:
    """Freeze the sync-time attested-identity snapshot (ADR-0019 §2/§4).

    Attested users only — an unattested email maps to nothing. Built by the
    framework and handed to the connector via the run context; connector code
    never reads the DB.
    """
    async with tenant_session_scope(tenant_id) as session:
        emails = await UserRepository(session, tenant_id).attested_email_map()
    return AclMappingContext(email_to_user_id=emails, evaluated_at=datetime.now(UTC))


# Bounded default timeout for an authenticated connector run's client.
_RUN_TIMEOUT_SECONDS = 30.0


async def _build_run(
    connector: Connector,
    source: Source,
    *,
    tenant_id: UUID,
    settings: Settings,
    acl_context: AclMappingContext | None = None,
) -> ConnectorRun:
    """Construct the per-run execution context (ADR-0019 §4) — framework-only.

    Credential-less connectors get an anonymous bounded client. For an OAuth
    connector the framework resolves the vault-held refresh token
    (``get_secret_plaintext`` with the **system** accessor, so ``secret.accessed``
    names the sync worker) and runs the refresh grant; the resulting access
    token exists only inside the returned client's default headers — connector
    code never sees it, and it is never persisted or logged.

    Raises:
        OAuthGrantDeadError: no usable credential (missing ``auth_secret_ref``
            or ``invalid_grant``) — the caller fails the sync closed as
            *reauthorize required*.
        OAuthExchangeError: a transient token-endpoint fault (retryable error).
    """
    spec = get_oauth_spec(connector)
    if spec is None:
        return ConnectorRun(
            http=httpx.AsyncClient(timeout=_RUN_TIMEOUT_SECONDS), acl_context=acl_context
        )
    if source.auth_secret_ref is None:
        raise OAuthGrantDeadError()

    def _vault(session: AsyncSession) -> SecretsService:
        # The worker acts as the SYSTEM under the tenant's authority: ADMIN role
        # so a source reconnected by a *different* admin (the credential row's
        # owner) still authorizes — `secret.accessed` names the true reader
        # (the system accessor), and RLS is armed by the tenant-bound scope.
        return build_secrets_service(
            session,
            settings=settings,
            tenant_id=tenant_id,
            owner_id=source.owner_id,
            roles=(Role.ADMIN,),
            audit=AuditSink(AuditEventRepository(session, tenant_id)),
            request_id="source-sync-task",
            source_ip="system",
        )

    async with tenant_session_scope(tenant_id) as session:
        try:
            refresh_token = await _vault(session).get_secret_plaintext(
                source.auth_secret_ref, accessor=AuditActor.system()
            )
        except NotFoundError as exc:
            # The credential row vanished (deleted secret; SET NULL raced) —
            # indistinguishable from a dead grant: reauthorize is the repair.
            raise OAuthGrantDeadError() from exc
    async with httpx.AsyncClient(timeout=_RUN_TIMEOUT_SECONDS) as token_http:
        token = await refresh_access_token(token_http, spec, refresh_token=refresh_token)
    if token.refresh_token is not None:
        # The provider rotated the refresh token on use — re-store it in place
        # (by reference, owner unchanged) or the NEXT sync would refresh with a
        # revoked credential (#452 AC).
        async with tenant_session_scope(tenant_id) as session:
            try:
                await _vault(session).rotate_secret_value(
                    source.auth_secret_ref,
                    plaintext=token.refresh_token,
                    accessor=AuditActor.system(),
                )
            except NotFoundError as exc:
                # The credential row was deleted between the read and this
                # rotation (source/secret deletion racing the sync) — the same
                # repair as any dead grant: reauthorize (terminal, audited).
                raise OAuthGrantDeadError() from exc
    # The authenticated client is framework-built and HOST-PINNED to the
    # connector's fixed allowlist (ADR-0019 §4/§5): an off-allowlist request
    # raises inside the guard before the Authorization header could leave.
    return ConnectorRun(
        http=build_authenticated_client(
            spec, access_token=token.access_token, timeout=_RUN_TIMEOUT_SECONDS
        ),
        acl_context=acl_context,
    )


async def _sync_incremental(
    tenant_id: UUID,
    source: Source,
    connector: Connector,
    run: ConnectorRun,
    *,
    settings: Settings,
    object_store: ObjectStore,
    gateway: LLMGateway,
    collection_id: UUID,
    enforce_acl: bool,
) -> SyncResult:
    """Drain the connector's change pages with per-page atomic commits (ADR-0019 §3).

    For each :class:`~app.connectors.base.SyncPage`, ONE tenant-scoped
    transaction applies, in order: identity **deletions** → the
    **stale-stamp** (the page's ``stale_scope_ids`` matched against persisted
    scope chains; ``integrity=incomplete`` ⇒ every ``acl_enforced`` document of
    the source) → identity **upserts** (which restore per-document freshness as
    each is re-examined) → ``sync_cursor = page.next_cursor``. A crash between
    pages therefore resumes from the last committed token — never skipping a
    page, never re-serving half-applied state.

    **An ``integrity=incomplete`` page is never consumed behind the cursor, and
    what it breaks is not repairable incrementally.** Its mutations and the
    source-wide stale stamp commit (the work is not lost), but in that same
    transaction the durable ``acl_resync_required`` state is set, the cursor is
    deliberately **left where it was**, and the replay stops there (a later
    page's commit would move the cursor past unrecovered work). The run
    terminates as ``error`` and publishes **no** fresh source-level ACL health.

    The requirement is *sticky* on purpose. The stamp nulls every mirrored
    document of the source; a later replay of that same page re-examines only
    the documents that page reports, so it can restore a subset and would
    otherwise publish READY while unrelated rows sit at ``acl_synced_at =
    NULL`` — invisible, but reported healthy. So only a **completed full sync**
    clears ``acl_resync_required``, and while it is set
    :func:`sync_source_async` skips the incremental path entirely.

    A **wholly complete** replay does the opposite: it attests the mirrors it
    did not touch (§2's "a complete gap-free replay attests it unchanged"),
    without reviving anything an incomplete run stamped stale — otherwise every
    untouched document would age out of the freshness window and vanish from
    retrieval while hourly replays kept succeeding.

    Chunk/embedding/index writes happen **after** each page commit (the reused
    ingestion pipeline owns its own transactions and is idempotent; a document
    stranded ``pending`` by a crash in that window is recovered by the poll
    beat's sweep — see :mod:`app.tasks.connector_poll`). The index stale-stamp
    propagates by update-by-query in the same run; the Postgres hydration
    re-check remains the enforcing backstop (ADR-0010 §4).

    Raises:
        CursorExpiredError: the provider rejected the cursor (HTTP 410) — the
            caller clears it and falls back to a full resync.
        ConnectorError: a replay fault; pages already committed stand.
    """
    fetch_changes = get_fetch_changes(connector)
    assert fetch_changes is not None  # capability checked by the caller  # noqa: S101
    source_id = source.id
    owner_id = source.owner_id
    upserted = 0
    deleted_count = 0
    incomplete_run = False
    # Every document this run re-examined — excluded from the unchanged
    # attestation below because they already carry their own fresh stamp.
    examined_ids: set[UUID] = set()

    async for page in fetch_changes(source, str(source.sync_cursor), run):
        deleted_docs: list[Document] = []
        page_doc_ids: list[UUID] = []
        stale_ids: list[UUID] = []
        orphaned_keys: list[str] = []
        # Objects written by this page: `replaced` are prior revisions this
        # page superseded (deletable once the row no longer points at them),
        # `written` are the new ones (deletable only if the transaction dies).
        replaced_keys: set[str] = set()
        written_keys: set[str] = set()
        incomplete_page = page.integrity is PageIntegrity.INCOMPLETE
        try:
            async with tenant_session_scope(tenant_id) as session:
                documents = DocumentRepository(session, tenant_id)
                sources = SourceRepository(session, tenant_id)

                # 1. Identity deletions (reconcile by external id, not wholesale).
                for external_id in sorted(page.deleted_external_ids):
                    existing = await documents.get_by_external_id(source_id, external_id)
                    if existing is None:
                        continue
                    await documents.delete(existing.id)
                    deleted_docs.append(existing)
                if deleted_docs:
                    await session.flush()
                    for storage_key in {d.storage_key for d in deleted_docs}:
                        if await documents.count_by_storage_key(storage_key) == 0:
                            orphaned_keys.append(storage_key)

                # 2. The cascade stale-stamp (§3): immediate deny for every known
                #    descendant of a replayed container change — BEFORE the page's
                #    upserts, so a re-examined document ends the page fresh.
                if page.stale_scope_ids:
                    stale_ids = await documents.stamp_acl_stale_by_scope(
                        source_id, page.stale_scope_ids
                    )
                if incomplete_page:
                    # Unprovable affected set ⇒ fail closed source-wide.
                    stale_ids = await documents.stamp_acl_stale_for_source(source_id)

                # 3. Identity upserts — by (source_id, external_id), keeping row
                #    ids stable so the index converges without orphans.
                for index, fetched_doc in enumerate(page.upserts):
                    payload = (
                        fetched_doc.data
                        if fetched_doc.data is not None
                        else fetched_doc.text.encode("utf-8")
                    )
                    mime_type = (
                        fetched_doc.mime_type if fetched_doc.data is not None else _TEXT_MIME
                    )
                    stored = await object_store.put(
                        tenant_id=str(tenant_id),
                        data=payload,
                        content_type=mime_type,
                        filename=_safe_filename(fetched_doc.title, index),
                    )
                    written_keys.add(stored.key)
                    acl = fetched_doc.acl
                    acl_stamp = datetime.now(UTC) if acl is not None else None
                    existing = (
                        await documents.get_by_external_id(source_id, fetched_doc.external_id)
                        if fetched_doc.external_id is not None
                        else None
                    )
                    if existing is not None:
                        if existing.storage_key != stored.key:
                            # This revision supersedes the prior object; the row
                            # will stop referencing it once this commits.
                            replaced_keys.add(existing.storage_key)
                        updated = await documents.update_from_sync(
                            existing.id,
                            filename=_safe_filename(fetched_doc.title, index),
                            mime_type=mime_type,
                            size_bytes=stored.size_bytes,
                            storage_key=stored.key,
                            status=DocumentStatus.PENDING,
                            acl_principals=sorted(acl.principals) if acl is not None else None,
                            acl_synced_at=acl_stamp,
                            acl_scope_ids=sorted(acl.scope_ids) if acl is not None else None,
                        )
                        assert updated is not None  # row read in this txn  # noqa: S101
                        page_doc_ids.append(existing.id)
                    else:
                        created = await documents.create(
                            owner_id=owner_id,
                            collection_id=collection_id,
                            filename=_safe_filename(fetched_doc.title, index),
                            mime_type=mime_type,
                            size_bytes=stored.size_bytes,
                            storage_key=stored.key,
                            acl_enforced=enforce_acl,
                            status=DocumentStatus.PENDING,
                            source_id=source_id,
                            external_id=fetched_doc.external_id,
                            acl_principals=sorted(acl.principals) if acl is not None else None,
                            acl_synced_at=acl_stamp,
                            acl_scope_ids=sorted(acl.scope_ids) if acl is not None else None,
                        )
                        page_doc_ids.append(created.id)

                # 4. The cursor — SAME transaction as the mutations. A complete
                #    page advances it (the stored token is the exact resume
                #    point). An INCOMPLETE page must not be consumed: the cursor
                #    stays put AND the durable full-resync requirement is
                #    recorded here, so neither a crash after this commit nor any
                #    number of later incremental retries can lose it.
                if not incomplete_page:
                    await sources.set_sync_cursor(source_id, page.next_cursor)
                else:
                    await sources.record_acl_resync_required(source_id, True)
        except Exception:
            # The transaction rolled back but the objects were already stored —
            # nothing references them, so reclaim them rather than leak
            # (post-rollback, best-effort, reference-checked like every other
            # object cleanup). Cleanup must never mask the real failure: if the
            # database is the thing that died, the reference check dies too.
            try:
                await _reclaim_objects(
                    tenant_id, written_keys, object_store=object_store, source_id=source_id
                )
            except Exception as cleanup_exc:  # noqa: BLE001 — cleanup is best-effort
                log.warning(
                    "source_sync.rollback_cleanup_failed",
                    source_id=str(source_id),
                    error=type(cleanup_exc).__name__,
                )
            raise

        # --- Post-commit, idempotent derived-state maintenance. --------------
        # Prior revisions are reclaimed only AFTER the commit, and only when no
        # surviving document still references the content-addressed key
        # (identical bytes+filename dedupe to one object).
        await _reclaim_objects(
            tenant_id,
            set(orphaned_keys) | (replaced_keys - written_keys),
            object_store=object_store,
            source_id=source_id,
        )
        for doc in deleted_docs:
            try:
                await sync_document_index_async(tenant_id, doc.id, settings=settings)
            except DependencyError as exc:
                log.warning(
                    "source_sync.index_cleanup_failed",
                    source_id=str(source_id),
                    document_id=str(doc.id),
                    error=exc.code,
                )
        if stale_ids:
            try:
                await stamp_index_acl_stale_async(tenant_id, stale_ids, settings=settings)
            except DependencyError as exc:
                # The Postgres stamp already committed; the hydration re-check
                # is the enforcing backstop until the reindex repairs this.
                log.warning(
                    "source_sync.index_stale_stamp_failed",
                    source_id=str(source_id),
                    error=exc.code,
                )
        for document_id in page_doc_ids:
            try:
                outcome = await ingest_document_async(
                    tenant_id,
                    document_id,
                    settings=settings,
                    object_store=object_store,
                    gateway=gateway,
                )
            except Exception as exc:  # noqa: BLE001 — one bad doc must not fail the sync
                log.warning(
                    "source_sync.doc_failed",
                    source_id=str(source_id),
                    document_id=str(document_id),
                    error=type(exc).__name__,
                )
                continue
            if outcome.status is DocumentStatus.READY:
                upserted += 1
            else:
                log.warning(
                    "source_sync.doc_terminal_failure",
                    source_id=str(source_id),
                    document_id=str(document_id),
                    status=outcome.status.value,
                )
        deleted_count += len(deleted_docs)
        examined_ids.update(page_doc_ids)
        if incomplete_page:
            # Stop draining: the cursor still points at THIS page, and any
            # later page's commit would advance it past unrecovered work.
            incomplete_run = True
            break

    # --- Terminal ------------------------------------------------------------
    now = datetime.now(UTC)
    attested_ids: list[UUID] = []
    # An outstanding full-resync requirement — this run's, or one committed by
    # an earlier run — blocks the healthy terminal. No incremental replay can
    # satisfy it: it re-examines the documents its pages report, never the whole
    # corpus a source-wide stale stamp nulled. (The caller already refuses to
    # enter the incremental path while the requirement stands; this is the
    # second lock on the same door, so no future caller can publish READY over
    # rows that are still denied.)
    unrecovered = incomplete_run or source.acl_resync_required
    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        source_documents = await documents.list_for_source(source_id)
        indexed = sum(1 for document in source_documents if document.status is DocumentStatus.READY)
        failed_documents = len(source_documents) - indexed
        sources = SourceRepository(session, tenant_id)

        if not unrecovered and not failed_documents and enforce_acl:
            # Attest FIRST — a complete, gap-free replay proves the documents it
            # did not report are unchanged, and this can only ever NARROW the
            # stale set (it never revives a NULL stamp; see
            # ``attest_acl_unchanged``).
            attested_ids = await documents.attest_acl_unchanged(
                source_id, attested_at=now, exclude_ids=examined_ids
            )
            # ...then PROVE the mirror is whole before claiming health. A page
            # can be `complete` and still leave rows stale: a cascade
            # stale-stamps every descendant of a changed container, and the run
            # may re-examine only a subset of them. Publishing a fresh
            # source-level `acl_synced_at` over such a row would advertise
            # health for content the permission predicate is denying. Any
            # survivor demotes this run and commits the durable requirement, so
            # the next run is a full sync.
            stale_remaining = await documents.count_stale_acl(source_id)
            if stale_remaining:
                unrecovered = True
                attested_ids = []
                await sources.record_acl_resync_required(source_id, True)
                log.warning(
                    "source_sync.acl_mirror_incomplete",
                    source_id=str(source_id),
                    stale_documents=stale_remaining,
                )

        if failed_documents:
            failure_reason = (
                f"{failed_documents} of {len(source_documents)} source document(s) "
                "failed or remain non-ready"
            )
            await sources.update_status(
                source_id,
                status=SourceStatus.ERROR,
                indexed_count=indexed,
                last_error=failure_reason,
                set_last_error=True,
            )
            if enforce_acl:
                unmapped = await _count_unmapped(session, tenant_id, source_id)
                await sources.record_acl_health(
                    source_id, acl_synced_at=None, unmapped_acl_count=unmapped
                )
        elif unrecovered:
            # An unrecovered mirror is NOT a healthy source: no ready status, no
            # fresh source-level acl_synced_at. Every acl_enforced document is
            # already stamped stale (denied) and `acl_resync_required` is
            # committed, so the next run is a full resync.
            await sources.update_status(
                source_id,
                status=SourceStatus.ERROR,
                indexed_count=indexed,
                last_error=ACL_MIRROR_INCOMPLETE,
                set_last_error=True,
            )
            if enforce_acl:
                unmapped = await _count_unmapped(session, tenant_id, source_id)
                await sources.record_acl_health(
                    source_id, acl_synced_at=None, unmapped_acl_count=unmapped
                )
        else:
            await sources.update_status(
                source_id,
                status=SourceStatus.READY,
                indexed_count=indexed,
                last_error=None,
                set_last_error=True,
                last_synced_at=now,
                set_last_synced_at=True,
            )
            if enforce_acl:
                # Attestation already ran above, and the mirror was PROVEN whole
                # (no `acl_enforced` row left with a NULL stamp) — only then is a
                # fresh source-level `acl_synced_at` an honest claim.
                unmapped = await _count_unmapped(session, tenant_id, source_id)
                await sources.record_acl_health(
                    source_id, acl_synced_at=now, unmapped_acl_count=unmapped
                )
            # NOTE: `acl_resync_required` is deliberately NOT cleared here. A
            # complete *incremental* replay proves only that the documents its
            # pages reported are current — never that the corpus a source-wide
            # stale stamp nulled has been restored. Only the full-sync terminal
            # clears it (and this branch cannot even run while it is set).

    if attested_ids:
        try:
            await attest_index_acl_fresh_async(
                tenant_id, attested_ids, synced_at=now, settings=settings
            )
        except DependencyError as exc:
            # Postgres is authoritative and its hydration re-check re-evaluates
            # freshness, so a failed propagation only costs engine recall until
            # the reindex backfill catches up.
            log.warning(
                "source_sync.index_attest_failed",
                source_id=str(source_id),
                error=exc.code,
            )
    if failed_documents:
        failure_reason = (
            f"{failed_documents} of {failed_documents + indexed} source document(s) "
            "failed or remain non-ready"
        )
        log.warning(
            "source_sync.incremental_document_failures",
            source_id=str(source_id),
            ready_documents=indexed,
            failed_documents=failed_documents,
        )
        return SyncResult(source_id, SourceStatus.ERROR, indexed, failure_reason)
    if unrecovered:
        log.warning(
            "source_sync.incremental_incomplete",
            source_id=str(source_id),
            full_resync_required=True,
        )
        return SyncResult(source_id, SourceStatus.ERROR, indexed, ACL_MIRROR_INCOMPLETE)
    log.info(
        "source_sync.incremental_done",
        source_id=str(source_id),
        upserted=upserted,
        deleted=deleted_count,
        attested=len(attested_ids),
    )
    return SyncResult(source_id, SourceStatus.READY, indexed)


async def _reclaim_objects(
    tenant_id: UUID,
    storage_keys: set[str],
    *,
    object_store: ObjectStore,
    source_id: UUID,
) -> None:
    """Delete stored objects no surviving document references (#269, ADR-0019 §3).

    Content-addressed keys are shared by identical bytes+filename, so every
    candidate is reference-checked against the **committed** state before it is
    removed — never a row pointing at missing bytes. Best-effort: a storage
    blip must not fail an otherwise-good sync, and a stray object is a benign
    leak.
    """
    if not storage_keys:
        return
    unreferenced: list[str] = []
    async with tenant_session_scope(tenant_id) as session:
        documents = DocumentRepository(session, tenant_id)
        for storage_key in sorted(storage_keys):
            if await documents.count_by_storage_key(storage_key) == 0:
                unreferenced.append(storage_key)
    for storage_key in unreferenced:
        try:
            await object_store.delete(str(tenant_id), storage_key)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            log.warning(
                "source_sync.object_cleanup_failed",
                source_id=str(source_id),
                storage_key=storage_key,
                error=type(exc).__name__,
            )


async def _fail_reauthorize(tenant_id: UUID, source_id: UUID, *, source_type: str) -> SyncResult:
    """Fail a sync closed on a dead OAuth grant (ADR-0019 §1).

    Marks the source ``error`` with the ``reauthorize_required`` sentinel and
    emits the **sync-stage** audit — ``source.synced`` with ``outcome=error``
    from the system actor (never ``source.connected``, which is callback-stage
    only). The repair is the connect flow, surfaced by the health field. The
    tenant-bound scope arms the RLS backstop (a plain ``session_scope`` would
    see no rows under the FORCE-RLS deployment role).
    """
    async with tenant_session_scope(tenant_id) as session:
        await SourceRepository(session, tenant_id).update_status(
            source_id,
            status=SourceStatus.ERROR,
            indexed_count=0,
            last_error=REAUTHORIZE_REQUIRED,
            set_last_error=True,
        )
        await AuditSink(AuditEventRepository(session, tenant_id)).emit(
            action=AuditAction.SOURCE_SYNCED,
            actor=AuditActor.system(),
            resource_type="source",
            resource_id=str(source_id),
            outcome=AuditOutcome.ERROR,
            request_id="source-sync-task",
            source_ip="system",
            metadata={"type": source_type, "reason": REAUTHORIZE_REQUIRED},
        )
    return SyncResult(source_id, SourceStatus.ERROR, 0, REAUTHORIZE_REQUIRED)


async def _fail(
    tenant_id: UUID,
    source_id: UUID,
    reason: str,
    *,
    indexed_count: int = 0,
) -> SyncResult:
    """Mark a source ``error`` with ``reason`` (own transaction). ADR-0009 §4.

    ``indexed_count=0`` is written explicitly: the failed sync already deleted the
    source's prior documents (Phase 3), so a stale non-zero count from an earlier
    successful sync must not survive (e.g. "error / 2 indexed" with an empty doc
    list). ``update_status`` leaves ``indexed_count`` untouched unless supplied (#157).
    """
    async with tenant_session_scope(tenant_id) as session:
        await SourceRepository(session, tenant_id).update_status(
            source_id,
            status=SourceStatus.ERROR,
            indexed_count=indexed_count,
            last_error=reason,
            set_last_error=True,
        )
    return SyncResult(source_id, SourceStatus.ERROR, indexed_count, reason)


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
    loop via :func:`app.tasks.runner.run_task` (which disposes the DB engine after
    each run so no pooled connection outlives its loop, #140), and returns the
    result dict. A fetch/SSRF fault is already recorded as
    ``error`` on the source by the async core (returned, not raised), so the task
    does not retry a permanently-blocked URL. Args are strings (Celery serializes
    JSON, not UUIDs); parsed back to ``UUID`` here.
    """
    settings = get_settings()
    tid = UUID(tenant_id)
    sid = UUID(source_id)
    object_store = ObjectStore(settings)
    gateway = LLMGateway(settings)

    async def _run() -> SyncResult:
        # Connector ingestion uses the same vector space as uploads. Validate
        # the complete DB/index/provider contract before claiming the source or
        # creating any documents, so a drifted worker fails closed (R1-005/006).
        await ensure_embedding_contract(settings)
        return await sync_source_async(
            tid,
            sid,
            settings=settings,
            object_store=object_store,
            gateway=gateway,
        )

    try:
        result = run_task(_run())
    except DependencyError as exc:
        # Contract/dependency faults before the source core claims its row are
        # retryable. Use the same bounded exponential policy as document
        # ingestion, then publish an explicit terminal Error so a broker-
        # accepted source can never remain Pending forever (R2-002). Persist
        # only the stable adapter code, never provider detail/payload.
        retries = int(getattr(getattr(self, "request", None), "retries", 0) or 0)
        if retries < settings.ingestion_max_retries:
            countdown = settings.ingestion_retry_backoff_seconds * (2**retries)
            raise self.retry(exc=exc, countdown=countdown) from exc  # type: ignore[attr-defined]
        reason = (
            "Embedding contract preflight failed after "
            f"{settings.ingestion_max_retries} retries ({exc.code})."
        )
        result = run_task(_fail(tid, sid, reason))
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
