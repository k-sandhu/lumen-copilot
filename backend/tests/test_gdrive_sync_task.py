"""Framework sync-task tests for the ACL-mirroring incremental path (#453).

Drives :func:`app.tasks.sync_source.sync_source_async` offline (in-memory
SQLite, fake object store/gateway/index store) against a **fake
capability-declaring connector** registered as ``gdrive``. What this module
owns is the sync *plumbing*: the per-page atomic mutation+cursor commits,
crash-between-pages resume, identity reconcile, the object lifecycle (replaced
revisions, rollback orphans, the stranded-ingestion sweep), 410 → full-resync
fallback, the sync-poll beat, and the wire's ``GdriveSource`` health surface
(contract-validated).

The **ACL deny-by-default semantics** of the same path — the mandatory
no-default write mode, the attested-identity snapshot, the §3 cascade
stale-stamp and ``integrity=incomplete`` source-wide stamp, the sticky
full-resync requirement, and source-side revocation — moved to
``tests/acl_kit/`` (#454), where they run against **every** ACL-declaring
connector rather than only this one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
from app.connectors.base import (
    AclMappingContext,
    ConnectorError,
    ConnectorHealth,
    ConnectorRun,
    CursorExpiredError,
    FetchedDoc,
    FullSyncResult,
    PageIntegrity,
    SourceAcl,
    SyncPage,
)
from app.core.config import Settings
from app.db import models
from app.db.base import Base
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    SourceReconcileRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.db.session import session_scope
from app.db.tenant_context import bind_bypass
from app.domain.entities import DocumentStatus, Role, Source, SourceStatus
from app.domain.llm import Embedding
from app.search.filters import acl_freshness_floor
from app.tasks.sync_source import sync_source_async

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_DIM = 8
sync_source_module = import_module("app.tasks.sync_source")

_SPEC = Path(__file__).resolve().parent.parent.parent / "contracts" / "openapi.yaml"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
        "CELERY_BROKER_URL": "redis://localhost:6379/1",
        "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
        "S3_ENDPOINT_URL": "http://localhost:9000",
        "S3_ACCESS_KEY": "k",
        "S3_SECRET_KEY": "s",
        "S3_BUCKET": "b",
        "OPENROUTER_API_KEY": "",
        "INGESTION_CHUNK_SIZE": "120",
        "INGESTION_CHUNK_OVERLAP": "20",
        "INGESTION_EMBED_BATCH_SIZE": "3",
        "LLM_EMBEDDING_DIMENSIONS": str(_DIM),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _FakeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, *, tenant_id: str, data: bytes, content_type: str, filename: str):  # noqa: ANN201
        from app.storage.keys import build_key
        from app.storage.object_store import StoredObject

        key = build_key(tenant_id, data, filename)
        self.objects[key] = data
        return StoredObject(
            key=key, sha256=key.split("/")[1], size_bytes=len(data), content_type=content_type
        )

    async def get(self, tenant_id: str, key: str) -> bytes:
        return self.objects[key]

    async def delete(self, tenant_id: str, key: str) -> None:
        self.objects.pop(key, None)


class _FakeGateway:
    async def embed(
        self,
        inputs: Sequence[str],
        *,
        model: str | None = None,
        cache_namespace: str | None = None,
    ) -> list[Embedding]:
        return [Embedding(vector=[float(len(t) % 5)] * _DIM, model="fake") for t in inputs]


class _FakeIndexStore:
    """Records index ops incl. the ACL stale-stamp update-by-query."""

    events: ClassVar[list[tuple[str, object]]] = []

    @classmethod
    def from_settings(cls, settings: object) -> _FakeIndexStore:
        return cls()

    async def ensure_index(self) -> None:
        return None

    async def upsert_chunks(self, chunks: Sequence[object], *, refresh: bool = False) -> None:
        for chunk in chunks:
            type(self).events.append(("upsert", chunk.document_id))  # type: ignore[attr-defined]

    async def delete_document(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID, refresh: bool = False
    ) -> None:
        type(self).events.append(("delete", document_id))

    async def stamp_acl_stale(
        self,
        *,
        tenant_id: uuid.UUID,
        scope_ids: Sequence[str] | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
        refresh: bool = False,
    ) -> None:
        type(self).events.append(("stamp", frozenset(document_ids or ())))

    async def attest_acl_fresh(
        self,
        *,
        tenant_id: uuid.UUID,
        document_ids: Sequence[uuid.UUID],
        synced_at: object,
        refresh: bool = False,
    ) -> None:
        type(self).events.append(("attest", frozenset(document_ids)))

    async def aclose(self) -> None:
        return None


class FakeAclConnector:
    """A ``map_acl`` + ``fetch_changes`` capability connector double."""

    name = "gdrive"

    def __init__(self) -> None:
        self.full_result = FullSyncResult(docs=(), baseline_cursor="baseline-1")
        # cursor -> list of SyncPage | Exception (raised mid-iteration).
        self.script: dict[str, list[SyncPage | Exception]] = {}
        self.fetch_calls: list[str] = []
        self.seen_ctx: AclMappingContext | None = None
        self.sync_calls = 0

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        return dict(config)

    async def sync(self, source: Source, run: ConnectorRun) -> FullSyncResult:
        self.sync_calls += 1
        self.seen_ctx = run.acl_context
        return self.full_result

    async def health(self, source: Source, run: ConnectorRun) -> ConnectorHealth:
        return ConnectorHealth(healthy=True)

    def map_acl(self, raw: dict[str, object], ctx: AclMappingContext) -> frozenset[str]:
        principals = raw.get("principals")
        return frozenset(principals) if isinstance(principals, list) else frozenset()

    async def fetch_changes(
        self, source: Source, cursor: str, run: ConnectorRun
    ) -> AsyncIterator[SyncPage]:
        self.seen_ctx = run.acl_context
        self.fetch_calls.append(cursor)
        for item in self.script.get(cursor, []):
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.fixture(autouse=True)
def _offline_index_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeIndexStore.events = []
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", _FakeIndexStore)


@pytest.fixture(autouse=True)
def _no_broker_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.sources_service._dispatch_off_loop", lambda fn, *, name: fn())
    monkeypatch.setattr("app.tasks.enqueue_source_sync", lambda *a, **k: None)
    monkeypatch.setattr("app.tasks.enqueue_index_sync", lambda *a, **k: None)


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch) -> FakeAclConnector:
    fake = FakeAclConnector()

    def _get(source_type: str) -> object:
        assert source_type == "gdrive"
        return fake

    monkeypatch.setattr(sync_source_module, "get_connector", _get)
    return fake


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[None]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    prev_engine = db_session._engine
    prev_maker = db_session._sessionmaker
    db_session._engine = engine
    db_session._sessionmaker = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    try:
        yield
    finally:
        db_session._engine = prev_engine
        db_session._sessionmaker = prev_maker
        await engine.dispose()


class _Seeded:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    member_id: uuid.UUID
    source_id: uuid.UUID
    collection_id: uuid.UUID


async def _seed(*, cursor: str | None = None, attest_owner: bool = True) -> _Seeded:
    s = _Seeded()
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        s.tenant_id = tenant.id
        users = UserRepository(session, tenant.id)
        owner = await users.create(email="owner@acme.test", password_hash="h", roles=[Role.ADMIN])
        member = await users.create(
            email="member@acme.test", password_hash="h", roles=[Role.MEMBER]
        )
        s.owner_id, s.member_id = owner.id, member.id
        if attest_owner:
            await users.attest_email(owner.id, attested_by=owner.id)
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=owner.id, name="gdrive: my_drive"
        )
        s.collection_id = collection.id
        source = await SourceRepository(session, tenant.id).create(
            owner_id=owner.id,
            type="gdrive",
            config={"mode": "my_drive", "collection_id": str(collection.id)},
            status=SourceStatus.PENDING,
        )
        s.source_id = source.id
        if cursor is not None:
            await SourceRepository(session, tenant.id).set_sync_cursor(source.id, cursor)
        await session.commit()
    return s


async def _run(
    seeded: _Seeded,
    store: _FakeObjectStore | None = None,
    *,
    settings: Settings | None = None,
) -> object:
    return await sync_source_async(
        seeded.tenant_id,
        seeded.source_id,
        settings=settings or _settings(),
        object_store=store or _FakeObjectStore(),  # type: ignore[arg-type]
        gateway=_FakeGateway(),  # type: ignore[arg-type]
    )


def _doc(
    external_id: str,
    *,
    principals: list[str] | None = None,
    scopes: list[str] | None = None,
    text: str = "The quick brown fox jumps over the lazy dog. " * 6,
) -> FetchedDoc:
    return FetchedDoc(
        title=f"Doc {external_id}",
        text=text,
        url=f"https://drive.google.com/open?id={external_id}",
        external_id=external_id,
        acl=SourceAcl(principals=frozenset(principals or []), scope_ids=frozenset(scopes or [])),
    )


async def _rows(seeded: _Seeded) -> dict[str, models.Document]:
    async with db_session.session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(models.Document).where(models.Document.source_id == seeded.source_id)
                )
            )
            .scalars()
            .all()
        )
        return {str(r.external_id): r for r in rows}


async def _source_row(seeded: _Seeded) -> models.Source:
    async with db_session.session_scope() as session:
        return (
            await session.execute(select(models.Source).where(models.Source.id == seeded.source_id))
        ).scalar_one()


# --- full sync: baseline cursor + the persisted scope chain -------------------


async def test_full_sync_persists_the_scope_chain_and_baseline_cursor(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """The §3 mechanics of a full run: the pre-enumeration start token becomes
    the source cursor, and each document keeps the container scope chain a later
    cascade matches against.

    The ACL *semantics* of this write — the mandatory ``acl_enforced`` mode, the
    mirrored principal set and the unmapped-ACL count — are proven for every
    ACL-declaring connector by ``tests/acl_kit/test_write_mode.py`` (#454).
    """
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(
            _doc("f1", principals=[f"user:{seeded.member_id}"], scopes=["drv1", "folder1"]),
            _doc("f2", principals=[]),
        ),
        baseline_cursor="baseline-42",
    )
    result = await _run(seeded)
    assert result.status is SourceStatus.READY  # type: ignore[attr-defined]

    rows = await _rows(seeded)
    assert set(rows) == {"f1", "f2"}
    assert rows["f1"].acl_scope_ids == ["drv1", "folder1"]

    source = await _source_row(seeded)
    assert source.sync_cursor == "baseline-42"  # start-token-before-enumeration
    assert source.acl_synced_at is not None


# --- incremental: per-page commits, identity reconcile, crash-resume ----------


async def test_incremental_upserts_deletes_and_advances_cursor_per_page(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(_doc("keep", principals=["tenant"]), _doc("gone", principals=["tenant"])),
        baseline_cursor="cur-1",
    )
    await _run(seeded)
    before = await _rows(seeded)
    keep_id = before["keep"].id

    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(
                    _doc("keep", principals=[f"user:{seeded.member_id}"], text="fresh " * 30),
                ),
                deleted_external_ids=frozenset({"gone"}),
                next_cursor="cur-2",
            ),
            SyncPage(
                upserts=(_doc("new", principals=["tenant"]),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-2",
            ),
        ]
    }
    result = await _run(seeded)
    assert result.status is SourceStatus.READY  # type: ignore[attr-defined]
    assert connector.fetch_calls == ["cur-1"]

    rows = await _rows(seeded)
    assert set(rows) == {"keep", "new"}
    # Identity reconcile: the upsert kept the SAME row id (no delete-recreate).
    assert rows["keep"].id == keep_id
    assert rows["keep"].acl_principals == [f"user:{seeded.member_id}"]
    source = await _source_row(seeded)
    assert source.sync_cursor == "baseline-2"  # the drained replay's baseline
    assert source.status == "ready"
    assert source.indexed_count == 2
    # The deleted document's index entries were cleared.
    assert ("delete", before["gone"].id) in _FakeIndexStore.events


async def test_incremental_crash_between_pages_resumes_exactly(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """Kill the replay after page 1 commits: the stored cursor IS page 1's
    next_cursor, page 1's mutations stand, and the rerun resumes from there —
    never re-serving or skipping a page."""
    seeded = await _seed()
    connector.full_result = FullSyncResult(docs=(), baseline_cursor="cur-1")
    await _run(seeded)

    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("p1", principals=["tenant"]),),
                deleted_external_ids=frozenset(),
                next_cursor="cur-2",
            ),
            ConnectorError("simulated crash between pages"),
        ],
        "cur-2": [
            SyncPage(
                upserts=(_doc("p2", principals=["tenant"]),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-3",
            ),
        ],
    }
    crashed = await _run(seeded)
    assert crashed.status is SourceStatus.ERROR  # type: ignore[attr-defined]
    source = await _source_row(seeded)
    assert source.sync_cursor == "cur-2"  # the exact resume point
    assert set(await _rows(seeded)) == {"p1"}  # page 1 committed, page 2 never applied

    resumed = await _run(seeded)
    assert resumed.status is SourceStatus.READY  # type: ignore[attr-defined]
    assert connector.fetch_calls == ["cur-1", "cur-2"]  # resumed, not restarted
    assert set(await _rows(seeded)) == {"p1", "p2"}
    assert (await _source_row(seeded)).sync_cursor == "baseline-3"


# --- integrity=incomplete: never consumed behind the cursor ------------------


def _utc(value: datetime) -> datetime:
    """Offline SQLite drops tzinfo; every stamp we write is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _backdate_acl(seeded: _Seeded, external_id: str, *, hours: int) -> None:
    """Age one document's mirror stamp (crossing the freshness window)."""
    from sqlalchemy import update as sa_update

    stale = datetime.now(UTC) - timedelta(hours=hours)
    async with db_session.session_scope() as session:
        await session.execute(
            sa_update(models.Document)
            .where(
                models.Document.source_id == seeded.source_id,
                models.Document.external_id == external_id,
            )
            .values(acl_synced_at=stale)
        )
        await session.commit()


def _null_run() -> ConnectorRun:
    """A run context for driving ``_sync_incremental`` directly (no HTTP)."""
    import httpx

    return ConnectorRun(http=httpx.AsyncClient(), acl_context=None)


def _incomplete_page(next_cursor: str = "baseline-x") -> SyncPage:
    return SyncPage(
        upserts=(),
        deleted_external_ids=frozenset(),
        next_cursor=next_cursor,
        integrity=PageIntegrity.INCOMPLETE,
    )


async def test_incomplete_page_does_not_advance_the_cursor_or_publish_health(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """Regression (ADR-0019 §3): an ``integrity=incomplete`` page was stamped
    stale but its cursor still committed, and the terminal path then marked the
    source ``ready`` with a FRESH source-level ACL timestamp.

    A persistent permission-fetch failure was therefore consumed behind the
    cursor — no replay would ever revisit it — while health reported success.
    The page must instead stay the resume point, the source must not be
    published ready/fresh, and the full-resync requirement must be durable.
    """
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(_doc("a", principals=["tenant"]),), baseline_cursor="cur-1"
    )
    await _run(seeded)

    connector.script = {"cur-1": [_incomplete_page()]}
    result = await _run(seeded)

    assert result.status is SourceStatus.ERROR  # type: ignore[attr-defined]
    assert result.error == "acl_mirror_incomplete"  # type: ignore[attr-defined]
    source = await _source_row(seeded)
    assert source.sync_cursor == "cur-1"  # NOT consumed
    assert source.status == "error"
    assert source.acl_synced_at is None  # health never lies about the mirror
    assert source.acl_resync_required is True  # durable, survives a crash
    assert (await _rows(seeded))["a"].acl_synced_at is None  # denied immediately


async def test_incomplete_replay_escalates_to_a_full_resync_and_recovers(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """An incomplete page stale-stamps the WHOLE source, so recovery is a full
    re-examination — the next run takes the full-sync path even though a cursor
    (and a perfectly good change page) is still there."""
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(_doc("a", principals=["tenant"]),), baseline_cursor="cur-1"
    )
    await _run(seeded)
    connector.script = {"cur-1": [_incomplete_page()]}

    failed = await _run(seeded)
    assert failed.status is SourceStatus.ERROR  # type: ignore[attr-defined]
    assert (await _source_row(seeded)).acl_resync_required is True

    # Next run: a complete page is available at the stored cursor, but the
    # outstanding requirement sends the run down the FULL path instead.
    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("a", principals=["tenant"]),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-2",
            )
        ]
    }
    calls_before, fetches_before = connector.sync_calls, list(connector.fetch_calls)
    connector.full_result = FullSyncResult(
        docs=(_doc("a", principals=["tenant"]),), baseline_cursor="cur-9"
    )
    recovered = await _run(seeded)

    assert recovered.status is SourceStatus.READY  # type: ignore[attr-defined]
    assert connector.sync_calls == calls_before + 1  # the FULL replay ran
    assert connector.fetch_calls == fetches_before  # the incremental path did not
    healthy = await _source_row(seeded)
    assert healthy.acl_resync_required is False  # cleared ONLY by the full replay
    assert healthy.acl_synced_at is not None  # health may report success again
    assert healthy.sync_cursor == "cur-9"
    assert (await _rows(seeded))["a"].acl_synced_at is not None


# --- complete replays attest unchanged mirrors -------------------------------


async def test_complete_replay_attests_unchanged_documents(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """Regression (ADR-0019 §2/§3): a complete, gap-free replay only advanced
    ``acl_synced_at`` on the documents it *changed*.

    With hourly successful replays and no changes, every untouched document
    aged past ``CONNECTOR_ACL_MAX_AGE_HOURS`` and silently disappeared from
    retrieval. A gap-free replay proves the untouched documents unchanged, so
    it must attest them — and propagate the freshness to the index.
    """
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(_doc("quiet", principals=["tenant"]),), baseline_cursor="cur-1"
    )
    await _run(seeded)
    await _backdate_acl(seeded, "quiet", hours=48)  # beyond the 24h window
    stale_row = (await _rows(seeded))["quiet"]
    assert stale_row.acl_synced_at is not None
    assert _utc(stale_row.acl_synced_at) < acl_freshness_floor()  # would be DENIED now

    connector.script = {
        "cur-1": [
            SyncPage(  # a wholly complete, no-change replay
                upserts=(),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-2",
            )
        ]
    }
    await _run(seeded)

    after = (await _rows(seeded))["quiet"]
    assert after.acl_synced_at is not None
    assert _utc(after.acl_synced_at) >= acl_freshness_floor()  # still retrievable
    # ...and the engine learns the same freshness (recall, never access).
    attested = [e[1] for e in _FakeIndexStore.events if e[0] == "attest"]
    assert any(after.id in ids for ids in attested)  # type: ignore[operator]


async def test_attestation_never_revives_a_stale_stamped_document(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """The rows a replay may never re-attest: ones a cascade stamped stale.

    The stamp is ``acl_synced_at = NULL``, and attestation only ever advances a
    non-NULL timestamp — so the distinction is structural, not bookkeeping.
    Uses the **scope cascade** (a container permission change), which is the
    stale-stamp an incremental run can legitimately recover from; the
    source-wide stamp is covered by the full-resync tests above.
    """
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(
            _doc("unrecovered", principals=["tenant"], scopes=["folderX"]),
            _doc("reexamined", principals=["tenant"], scopes=["folderX"]),
            _doc("elsewhere", principals=["tenant"], scopes=["folderY"]),
        ),
        baseline_cursor="cur-1",
    )
    await _run(seeded)

    # A container change stale-stamps folderX's descendants; the page
    # re-examines only ONE of them.
    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("reexamined", principals=["tenant"], scopes=["folderX"]),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-3",
                stale_scope_ids=frozenset({"folderX"}),
            )
        ]
    }
    result = await _run(seeded)
    after = await _rows(seeded)
    assert after["reexamined"].acl_synced_at is not None  # examined for real
    assert after["unrecovered"].acl_synced_at is None  # NOT revived by attestation
    assert after["elsewhere"].acl_synced_at is not None  # untouched scope, attested

    # The run may NOT call itself healthy while a mirrored row is still stale:
    # a `complete` page whose cascade stamped more descendants than it
    # re-examined leaves `unrecovered` denied, so publishing READY + a fresh
    # source-level acl_synced_at would advertise health for content the
    # permission predicate is (correctly) denying. The proof obligation demotes
    # the terminal and commits the durable full-resync requirement instead.
    assert result.status is SourceStatus.ERROR  # type: ignore[attr-defined]
    source = await _source_row(seeded)
    assert source.acl_resync_required
    assert source.acl_synced_at is None  # no fresh source-level stamp
    assert source.sync_cursor == "baseline-3"


# --- object lifecycle: replaced revisions + rollback orphans ------------------


async def test_replaced_object_is_reclaimed_on_an_incremental_update(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """Regression: an incremental update stored a new object and overwrote
    ``storage_key``, but only *deleted* rows contributed to the orphan sweep —
    so the prior object leaked on every single file revision."""
    store = _FakeObjectStore()
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(_doc("d1", principals=["tenant"], text="first revision " * 20),),
        baseline_cursor="cur-1",
    )
    await _run(seeded, store)
    [original_key] = list(store.objects)

    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("d1", principals=["tenant"], text="second revision " * 20),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-4",
            )
        ]
    }
    await _run(seeded, store)

    rows = await _rows(seeded)
    assert rows["d1"].storage_key in store.objects  # the live revision survives
    assert rows["d1"].storage_key != original_key
    assert original_key not in store.objects  # ...and the prior one is reclaimed


async def test_unchanged_content_keeps_its_object(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """Content-addressed keys are shared: an upsert that re-stores identical
    bytes must not delete the object the row still points at."""
    store = _FakeObjectStore()
    seeded = await _seed()
    same = _doc("d1", principals=["tenant"], text="stable body " * 20)
    connector.full_result = FullSyncResult(docs=(same,), baseline_cursor="cur-1")
    await _run(seeded, store)

    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(same,),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-5",
            )
        ]
    }
    await _run(seeded, store)

    rows = await _rows(seeded)
    assert rows["d1"].storage_key in store.objects


async def test_failed_page_transaction_reclaims_the_object_it_wrote(
    sqlite_engine: None, connector: FakeAclConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database failure AFTER ``put()`` used to leak the new object too: the
    row never referenced it, so no later sweep could ever find it."""

    class _BrokenDocuments(DocumentRepository):
        async def create(self, **kwargs: object) -> None:  # type: ignore[override]
            raise RuntimeError("database went away")

    store = _FakeObjectStore()
    seeded = await _seed(cursor="cur-1")
    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("d1", principals=["tenant"]),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-6",
            )
        ]
    }
    monkeypatch.setattr(sync_source_module, "DocumentRepository", _BrokenDocuments)

    with pytest.raises(RuntimeError):
        await _run(seeded, store)

    assert store.objects == {}  # the orphan was reclaimed, not leaked
    assert (await _source_row(seeded)).sync_cursor == "cur-1"  # nothing committed


# --- stranded-ingestion recovery (the crash window) --------------------------


async def test_crash_between_page_commit_and_ingestion_is_recovered_by_the_sweep(
    sqlite_engine: None, connector: FakeAclConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (ADR-0019 §3): the page's row + cursor commit, then ingestion
    runs post-commit. A worker killed in that window left a ``pending``
    document with no chunks that the advanced cursor would never revisit — and
    the reindex backfill cannot create chunks that were never parsed.

    The poll beat's sweep re-drives the idempotent ingestion task for connector
    documents stranded past ``CONNECTOR_INGEST_RECOVERY_MINUTES``.
    """
    from sqlalchemy import update as sa_update

    seeded = await _seed(cursor="cur-1")
    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("stranded", principals=["tenant"]),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-7",
            )
        ]
    }

    def _kill(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt("worker killed between commit and ingestion")

    monkeypatch.setattr(sync_source_module, "ingest_document_async", _kill)
    with pytest.raises(KeyboardInterrupt):
        await _run(seeded)

    # The crash window, exactly: committed row + advanced cursor, no chunks.
    rows = await _rows(seeded)
    stranded_id = rows["stranded"].id
    assert rows["stranded"].status == "pending"
    assert (await _source_row(seeded)).sync_cursor == "baseline-7"
    async with db_session.session_scope() as session:
        chunks = (
            (
                await session.execute(
                    select(models.Chunk).where(models.Chunk.document_id == stranded_id)
                )
            )
            .scalars()
            .all()
        )
    assert chunks == []

    # A document still inside the recovery window is left alone...
    poll_module = import_module("app.tasks.connector_poll")
    enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        poll_module, "enqueue_ingestion", lambda tid, did: enqueued.append((tid, did))
    )
    assert await poll_module._sweep_stranded(_settings()) == 0
    assert enqueued == []

    # ...and re-driven once it is provably stuck.
    async with db_session.session_scope() as session:
        await session.execute(
            sa_update(models.Document)
            .where(models.Document.id == stranded_id)
            .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
        )
        await session.commit()
    assert await poll_module._sweep_stranded(_settings()) == 1
    assert enqueued == [(seeded.tenant_id, stranded_id)]


async def test_sweep_ignores_non_connector_and_ready_documents(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep is connector-scoped and status-scoped: a plain upload pending
    ingestion (its own task owns it) and a ready connector document are both
    left alone."""
    from sqlalchemy import update as sa_update

    seeded = await _seed()
    async with db_session.session_scope() as session:
        documents = DocumentRepository(session, seeded.tenant_id)
        upload = await documents.create(
            owner_id=seeded.owner_id,
            collection_id=seeded.collection_id,
            filename="upload.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key="t/upload",
            acl_enforced=False,
        )
        done = await documents.create(
            owner_id=seeded.owner_id,
            collection_id=seeded.collection_id,
            filename="done.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key="t/done",
            acl_enforced=True,
            source_id=seeded.source_id,
            external_id="done",
            status=DocumentStatus.READY,
        )
        await session.execute(
            sa_update(models.Document)
            .where(models.Document.id.in_([upload.id, done.id]))
            .values(updated_at=datetime.now(UTC) - timedelta(hours=2))
        )
        await session.commit()

    poll_module = import_module("app.tasks.connector_poll")
    enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        poll_module, "enqueue_ingestion", lambda tid, did: enqueued.append((tid, did))
    )
    assert await poll_module._sweep_stranded(_settings()) == 0
    assert enqueued == []


async def test_cursor_expired_falls_back_to_full_resync(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(_doc("old", principals=["tenant"]),), baseline_cursor="cur-1"
    )
    await _run(seeded)
    assert connector.sync_calls == 1

    connector.script = {"cur-1": [CursorExpiredError()]}
    connector.full_result = FullSyncResult(
        docs=(_doc("fresh", principals=["tenant"]),), baseline_cursor="baseline-6"
    )
    result = await _run(seeded)
    assert result.status is SourceStatus.READY  # type: ignore[attr-defined]
    assert connector.sync_calls == 2  # 410 → cursor cleared → full resync ran
    assert set(await _rows(seeded)) == {"fresh"}  # full reconcile replaced
    assert (await _source_row(seeded)).sync_cursor == "baseline-6"


# --- wire: the GdriveSource health surface ------------------------------------


async def test_gdrive_source_wire_carries_acl_health(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    from app.api.v1.sources import _to_response
    from app.db.repositories import SourceRepository as _SR

    seeded = await _seed()
    connector.full_result = FullSyncResult(docs=(_doc("f1", principals=[]),), baseline_cursor="b-1")
    await _run(seeded)
    async with db_session.session_scope() as session:
        source = await _SR(session, seeded.tenant_id).get(seeded.source_id)
    assert source is not None
    payload = json.loads(_to_response(source).model_dump_json())
    assert payload["acl_synced_at"] is not None
    assert payload["unmapped_acl_count"] == 1

    spec = yaml.safe_load(_SPEC.read_text(encoding="utf-8"))
    schemas = dict(spec["components"]["schemas"])
    import jsonschema

    jsonschema.validate(payload, {**schemas["Source"], "components": {"schemas": schemas}})


# --- the sync-poll beat -------------------------------------------------------


async def test_poll_discovers_connected_resting_sources_and_enqueues(
    sqlite_engine: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed()
    secret_ref = uuid.uuid4()
    async with db_session.session_scope() as session:
        row = (
            await session.execute(select(models.Source).where(models.Source.id == seeded.source_id))
        ).scalar_one()
        row.auth_secret_ref = secret_ref  # SQLite: FK not enforced offline
        row.status = "ready"
        # A second, still-pending_auth source must NOT be polled.
        session.add(
            models.Source(
                tenant_id=seeded.tenant_id,
                owner_id=seeded.owner_id,
                type="gdrive",
                config={"mode": "my_drive"},
                status="pending_auth",
            )
        )
        await session.commit()

    async with session_scope() as session:
        await bind_bypass(session)
        pairs = await SourceReconcileRepository(session).list_connected_pollable()
    assert pairs == [(seeded.tenant_id, seeded.source_id)]

    poll_module = import_module("app.tasks.connector_poll")
    enqueued: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        poll_module, "enqueue_source_sync", lambda tid, sid: enqueued.append((tid, sid))
    )
    from app.tasks.connector_poll import _poll_all

    count = await _poll_all(_settings())
    assert count == 1
    assert enqueued == [(seeded.tenant_id, seeded.source_id)]


def test_beat_schedule_carries_connector_sync_poll() -> None:
    from app.core.config import get_settings
    from app.tasks.celery_app import celery_app
    from app.tasks.scheduler import configure_beat

    configure_beat(get_settings())
    entry: dict[str, Any] = celery_app.conf.beat_schedule["connector-sync-poll"]
    assert entry["task"] == "lumen.poll_connector_syncs"
    assert entry["schedule"] == float(get_settings().connector_sync_interval_minutes) * 60.0
