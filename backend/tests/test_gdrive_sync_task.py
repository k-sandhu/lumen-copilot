"""Framework sync-task tests for the ACL-mirroring incremental path (#453).

Drives :func:`app.tasks.sync_source.sync_source_async` offline (in-memory
SQLite, fake object store/gateway/index store) against a **fake
capability-declaring connector** registered as ``gdrive`` — the framework
seams under test are real: the mandatory write-mode argument, the per-page
atomic mutation+cursor commits, identity reconcile, the cascade stale-stamp
(Postgres NULL + index update-by-query), the ``integrity=incomplete``
source-wide stamp, 410 → full-resync fallback, the attested-identity snapshot,
and the wire's ``GdriveSource`` health surface (contract-validated).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Sequence
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
from app.domain.entities import Role, Source, SourceStatus
from app.domain.llm import Embedding
from app.tasks.sync_source import sync_source_async

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_DIM = 8
sync_source_module = import_module("app.tasks.sync_source")

_SPEC = Path(__file__).resolve().parent.parent.parent / "contracts" / "openapi.yaml"


def _settings() -> Settings:
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


async def _run(seeded: _Seeded, store: _FakeObjectStore | None = None) -> object:
    return await sync_source_async(
        seeded.tenant_id,
        seeded.source_id,
        settings=_settings(),
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


# --- full sync: write-mode discipline + baseline ------------------------------


async def test_full_sync_writes_acl_enforced_true_and_baseline_cursor(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(
            _doc("f1", principals=[f"user:{seeded.member_id}"], scopes=["drv1", "folder1"]),
            _doc("f2", principals=[]),  # empty mirror -> unmapped, invisible
        ),
        baseline_cursor="baseline-42",
    )
    result = await _run(seeded)
    assert result.status is SourceStatus.READY  # type: ignore[attr-defined]

    rows = await _rows(seeded)
    assert set(rows) == {"f1", "f2"}
    # THE write-mode assertion (ADR-0019 §2): every managed-source document is
    # written acl_enforced=true — a False here is a defect.
    for row in rows.values():
        assert row.acl_enforced is True
        assert row.acl_synced_at is not None  # examined this run
    assert rows["f1"].acl_principals == [f"user:{seeded.member_id}"]
    assert rows["f1"].acl_scope_ids == ["drv1", "folder1"]
    assert rows["f2"].acl_principals == []

    source = await _source_row(seeded)
    assert source.sync_cursor == "baseline-42"  # start-token-before-enumeration
    assert source.acl_synced_at is not None
    assert source.unmapped_acl_count == 1  # the empty-mirror doc, surfaced


async def test_write_seam_refuses_defaulted_acl_mode(
    sqlite_engine: None,
) -> None:
    """The ingestion seam's ACL-mode argument is mandatory/no-default."""
    seeded = await _seed()
    async with db_session.session_scope() as session:
        documents = DocumentRepository(session, seeded.tenant_id)
        with pytest.raises(TypeError):
            await documents.create(  # type: ignore[call-arg]
                owner_id=seeded.owner_id,
                collection_id=seeded.collection_id,
                filename="x.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key="t/x",
            )


async def test_acl_context_snapshot_is_attested_only(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """The framework freezes ONLY attested emails into the run's mapping
    context (ADR-0019 §2 — unattested maps to nothing)."""
    seeded = await _seed(attest_owner=True)
    await _run(seeded)
    assert connector.seen_ctx is not None
    assert connector.seen_ctx.email_to_user_id == {"owner@acme.test": seeded.owner_id}


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


async def test_stale_scope_stamp_denies_descendants_in_page_transaction(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """A replayed container change stale-stamps every known descendant (scope
    match on persisted acl_scope_ids) in the page transaction, propagates to
    the index by update-by-query, and an in-page re-examined doc ends fresh."""
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(
            _doc("inside", principals=["tenant"], scopes=["folderX"]),
            _doc("reexamined", principals=["tenant"], scopes=["folderX"]),
            _doc("outside", principals=["tenant"], scopes=["folderY"]),
        ),
        baseline_cursor="cur-1",
    )
    await _run(seeded)
    rows = await _rows(seeded)

    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("reexamined", principals=["tenant"], scopes=["folderX"]),),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-4",
                stale_scope_ids=frozenset({"folderX"}),
            ),
        ]
    }
    await _run(seeded)
    after = await _rows(seeded)
    assert after["inside"].acl_synced_at is None  # denied immediately
    assert after["reexamined"].acl_synced_at is not None  # refreshed in-run
    assert after["outside"].acl_synced_at is not None  # untouched scope
    # The index update-by-query got the stamped ids (scope-matched set).
    stamped = [e[1] for e in _FakeIndexStore.events if e[0] == "stamp"]
    assert any(rows["inside"].id in ids for ids in stamped)  # type: ignore[operator]


async def test_integrity_incomplete_stamps_source_wide(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(
            _doc("a", principals=["tenant"], scopes=["s1"]),
            _doc("b", principals=["tenant"], scopes=["s2"]),
        ),
        baseline_cursor="cur-1",
    )
    await _run(seeded)
    rows = await _rows(seeded)

    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(),
                deleted_external_ids=frozenset(),
                next_cursor="baseline-5",
                integrity=PageIntegrity.INCOMPLETE,
            ),
        ]
    }
    await _run(seeded)
    after = await _rows(seeded)
    assert after["a"].acl_synced_at is None
    assert after["b"].acl_synced_at is None
    stamped = [e[1] for e in _FakeIndexStore.events if e[0] == "stamp"]
    assert any({rows["a"].id, rows["b"].id} <= set(ids) for ids in stamped)  # type: ignore[arg-type]


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


async def test_source_side_revocation_enforced_after_next_sync(
    sqlite_engine: None, connector: FakeAclConnector
) -> None:
    """A revoked source ACL vanishes from the mirror on the next replay — the
    document stays but admits nobody (INV-2: excluded after next sync)."""
    seeded = await _seed()
    connector.full_result = FullSyncResult(
        docs=(_doc("d1", principals=[f"user:{seeded.member_id}"]),), baseline_cursor="cur-1"
    )
    await _run(seeded)
    assert (await _rows(seeded))["d1"].acl_principals == [f"user:{seeded.member_id}"]

    connector.script = {
        "cur-1": [
            SyncPage(
                upserts=(_doc("d1", principals=[]),),  # the source revoked it
                deleted_external_ids=frozenset(),
                next_cursor="baseline-7",
            ),
        ]
    }
    await _run(seeded)
    after = await _rows(seeded)
    assert after["d1"].acl_principals == []
    source = await _source_row(seeded)
    assert source.unmapped_acl_count == 1


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
