"""PR605 R4/R5: durable source finalization, retained counts, and rollback preservation.

The same assertions run offline and, with RUN_PR605_POSTGRES=1, on the real
migrated lumentest_pr605 database as a NOSUPERUSER/NOBYPASSRLS owner. Only
connector, storage, model, and search adapters are faked; task wrappers, transactions,
repositories, cascades, vector persistence, and downgrade are real.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Sequence
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from celery.exceptions import Retry
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.db.session as db_session
from app.connectors.base import FullSyncResult
from app.core.config import Settings, get_settings
from app.core.errors import DependencyError
from app.db import models
from app.db.repositories import (
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.entities import DocumentStatus, Role, SourceStatus
from app.domain.llm import Embedding
from tests._db_helpers import copy_sqlite_schema
from tests._live_helpers import isolated_live_url, worker_database_name
from tests.test_gdrive_sync_task import FakeAclConnector, _doc
from tests.test_sources_sync_task import _FakeIndexStore, _FakeObjectStore

sync_module = import_module("app.tasks.sync_source")
_wrapper = sync_module.sync_source.__wrapped__.__func__
_BACKEND = Path(__file__).resolve().parents[1]
_LIVE_URL = isolated_live_url(
    "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumentest_pr605"
)
_LIVE_DB = worker_database_name(_LIVE_URL.rsplit("/", 1)[-1])


def _migration_config() -> Config:
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    return config


@pytest_asyncio.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.live,
                pytest.mark.skipif(
                    os.environ.get("RUN_PR605_POSTGRES") != "1",
                    reason="set RUN_PR605_POSTGRES=1 for the isolated PostgreSQL R4 proof",
                ),
            ],
        ),
    ]
)
async def source_db(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AsyncIterator[bool]:
    live = request.param == "postgres"
    admin = None
    role = f"lumen_pr605_r4_{uuid4().hex}"
    if live:
        # Refuse to reset any shared app or other test database. Each live test
        # begins from the exact owner-approved DROP/CREATE pair.
        assert os.environ["DATABASE_URL"] == _LIVE_URL
        admin = create_async_engine(
            make_url(_LIVE_URL).set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        password = uuid4().hex
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE IF EXISTS {_LIVE_DB} WITH (FORCE)"))
            await conn.execute(text(f"CREATE DATABASE {_LIVE_DB}"))
            await conn.execute(
                text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS")
            )
            await conn.execute(text(f"ALTER DATABASE {_LIVE_DB} OWNER TO {role}"))
        provisioner = create_async_engine(_LIVE_URL)
        try:
            async with provisioner.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        finally:
            await provisioner.dispose()
        url = (
            make_url(_LIVE_URL)
            .set(username=role, password=password)
            .render_as_string(hide_password=False)
        )
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"

    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    # NullPool permits the real task runner's private loops and test read-back
    # loop to use fresh connections. No coroutine runner or transaction is mocked.
    monkeypatch.setattr(db_session, "get_sessionmaker", lambda *args, **kwargs: factory)
    try:
        if live:
            await asyncio.to_thread(command.upgrade, _migration_config(), "head")
            async with engine.connect() as conn:
                flags = (
                    await conn.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one()
                assert flags == (False, False)
        else:
            async with engine.begin() as conn:
                await conn.run_sync(copy_sqlite_schema)
        yield live
    finally:
        await engine.dispose()
        await db_session.dispose_engine()
        get_settings.cache_clear()
        if admin is not None:
            async with admin.connect() as conn:
                await conn.execute(text(f"DROP DATABASE IF EXISTS {_LIVE_DB} WITH (FORCE)"))
                await conn.execute(text(f"DROP ROLE {role}"))
            await admin.dispose()


async def _seed(source_type: str = "web") -> tuple[UUID, UUID]:
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="PR605 R4")
        await bind_tenant(session, tenant.id)
        user = await UserRepository(session, tenant.id).create(
            email="owner@pr605.test", password_hash="h", roles=[Role.ADMIN]
        )
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=user.id, name="R4 source"
        )
        source = await SourceRepository(session, tenant.id).create(
            owner_id=user.id,
            type=source_type,
            config={"collection_id": str(collection.id)},
            status=SourceStatus.PENDING,
        )
    return tenant.id, source.id


class _Task:
    def __init__(self, retries: int) -> None:
        self.request = SimpleNamespace(retries=retries, id="pr605-r4")
        self.calls: list[dict[str, object]] = []

    def retry(self, **kwargs: object) -> Retry:
        self.calls.append(kwargs)
        raise Retry("retrying", exc=kwargs["exc"])


@pytest.mark.parametrize("fault", ["update", "commit"])
async def test_source_terminal_write_redrives_until_database_recovers(
    source_db: bool, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """R3-001: even beyond work exhaustion, no ack before durable Error."""
    tenant_id, source_id = await _seed()
    settings = get_settings().model_copy(
        update={"ingestion_max_retries": 2, "ingestion_retry_backoff_seconds": 3}
    )
    monkeypatch.setattr(sync_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sync_module, "ObjectStore", lambda _: object())
    monkeypatch.setattr(sync_module, "LLMGateway", lambda _: object())
    preflight_calls = 0

    async def _preflight(_: Settings) -> str:
        nonlocal preflight_calls
        preflight_calls += 1
        if preflight_calls > 1:
            raise AssertionError(
                "terminal retry must not repeat work/preflight or replace its reason"
            )
        raise DependencyError("secret provider payload", code="embedding_dimension_mismatch")

    monkeypatch.setattr(sync_module, "ensure_embedding_contract", _preflight)
    original = SourceRepository.update_status if fault == "update" else AsyncSession.commit
    failed_calls = 0

    async def _db_down(*args: object, **kwargs: object) -> object:
        nonlocal failed_calls
        failed_calls += 1
        if fault == "commit":
            # Flush the actual terminal UPDATE, then fail commit so rollback
            # must restore Pending rather than just fail before any write.
            await args[0].flush()
        raise OperationalError("terminal write", {}, Exception("secret DB payload"))

    target = SourceRepository if fault == "update" else AsyncSession
    attribute = "update_status" if fault == "update" else "commit"
    monkeypatch.setattr(target, attribute, _db_down)
    retry_kwargs: dict[str, object] = {}
    for retries in (2, 3, 8):
        task = _Task(retries)
        with pytest.raises(Retry):
            await asyncio.to_thread(_wrapper, task, str(tenant_id), str(source_id), **retry_kwargs)
        [call] = task.calls
        assert isinstance(call["exc"], DependencyError)
        assert call["exc"].code == "source_finalize_database_error"
        assert "secret" not in str(call["exc"])
        assert call["countdown"] == 12  # capped finalizer backoff, no timing assertion
        retry_kwargs = call["kwargs"]
        assert "embedding_dimension_mismatch" in retry_kwargs["terminal_error"]
        assert "secret" not in retry_kwargs["terminal_error"]
    assert failed_calls == 3
    monkeypatch.setattr(target, attribute, original)
    async with db_session.tenant_session_scope(tenant_id) as session:
        pending = await SourceRepository(session, tenant_id).get(source_id)
        assert pending is not None and pending.status is SourceStatus.PENDING
    recovered = _Task(9)
    result = await asyncio.to_thread(
        _wrapper, recovered, str(tenant_id), str(source_id), **retry_kwargs
    )
    assert recovered.calls == []
    assert result["status"] == "error"
    assert preflight_calls == 1
    async with db_session.tenant_session_scope(tenant_id) as session:
        terminal = await SourceRepository(session, tenant_id).get(source_id)
        assert terminal is not None and terminal.status is SourceStatus.ERROR
        assert terminal.last_error == retry_kwargs["terminal_error"]
        assert terminal.indexed_count == 0


class _NativeGateway:
    async def embed(self, inputs: Sequence[str], **kwargs: object) -> list[Embedding]:
        return [Embedding(vector=[0.125] * 2048, model="fake-native") for _ in inputs]


@pytest.mark.parametrize("fault", [None, "read", "update", "commit"])
async def test_preclaim_resync_failure_preserves_retained_ready_count(
    source_db: bool, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    """R4-002: preflight exhaustion and terminal DB redrives retain existing truth."""
    tenant_id, source_id = await _seed()
    settings = get_settings().model_copy(
        update={"ingestion_max_retries": 2, "ingestion_retry_backoff_seconds": 3}
    )
    connector = FakeAclConnector()
    connector.name = "web"
    connector.full_result = FullSyncResult(
        docs=tuple(_doc(key, text=f"retained {key} passage") for key in ("d1", "d2")),
        baseline_cursor=None,
    )
    monkeypatch.setattr(sync_module, "get_map_acl", lambda _: None)
    monkeypatch.setattr(sync_module, "get_connector", lambda _: connector)
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", _FakeIndexStore)
    store = _FakeObjectStore()
    first = await sync_module.sync_source_async(
        tenant_id, source_id, settings=settings, object_store=store, gateway=_NativeGateway()
    )
    assert first.status is SourceStatus.READY and first.indexed_count == 2
    async with db_session.tenant_session_scope(tenant_id) as session:
        documents = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert len(documents) == 2 and all(d.status is DocumentStatus.READY for d in documents)
        before = (await session.execute(select(models.Chunk))).scalars().all()
        chunk_snapshot = {
            c.id: (c.document_id, c.text, list(c.embedding), c.embedding_fingerprint)
            for c in before
        }
        assert chunk_snapshot
        await SourceRepository(session, tenant_id).update_status(
            source_id, status=SourceStatus.SYNCING
        )
    index_operations = list(_FakeIndexStore.events)
    object_snapshot = dict(store.objects)
    monkeypatch.setattr(sync_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sync_module, "ObjectStore", lambda _: store)
    monkeypatch.setattr(sync_module, "LLMGateway", lambda _: _NativeGateway())
    preflight_calls = 0

    async def _preflight(_: Settings) -> str:
        nonlocal preflight_calls
        preflight_calls += 1
        assert preflight_calls == 1, "terminal redrive must skip preflight"
        raise DependencyError("secret provider payload", code="embedding_dimension_mismatch")

    monkeypatch.setattr(sync_module, "ensure_embedding_contract", _preflight)
    retry_kwargs: dict[str, object] = {}
    if fault is not None:
        target = AsyncSession if fault == "commit" else SourceRepository
        attribute = {"read": "get", "update": "update_status", "commit": "commit"}[fault]
        original = getattr(target, attribute)

        async def _db_down(*args: object, **kwargs: object) -> object:
            if fault == "commit":
                await args[0].flush()  # actual update must roll back
            raise OperationalError("terminal DB fault", {}, Exception("secret DB payload"))

        with monkeypatch.context() as failure_patch:
            failure_patch.setattr(target, attribute, _db_down)
            for retries in (2, 8):
                task = _Task(retries)
                with pytest.raises(Retry):
                    await asyncio.to_thread(
                        _wrapper, task, str(tenant_id), str(source_id), **retry_kwargs
                    )
                [call] = task.calls
                assert call["countdown"] == 12
                assert call["exc"].code == "source_finalize_database_error"
                retry_kwargs = call["kwargs"]
                # A failed read carries preservation intent (None); a failed
                # write carries the count already read in the fresh transaction.
                assert retry_kwargs["terminal_indexed_count"] == (None if fault == "read" else 2)
                assert "embedding_dimension_mismatch" in retry_kwargs["terminal_error"]
                assert "secret" not in str(call["exc"]) + retry_kwargs["terminal_error"]
        assert getattr(target, attribute) is original
        async with db_session.tenant_session_scope(tenant_id) as session:
            retained = await SourceRepository(session, tenant_id).get(source_id)
            assert retained.status is SourceStatus.SYNCING and retained.indexed_count == 2

    task = _Task(9 if fault else 2)
    result = await asyncio.to_thread(_wrapper, task, str(tenant_id), str(source_id), **retry_kwargs)
    assert task.calls == [] and preflight_calls == 1
    assert result["status"] == "error" and result["indexed_count"] == 2
    assert "embedding_dimension_mismatch" in result["error"] and "secret" not in result["error"]
    async with db_session.tenant_session_scope(tenant_id) as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        retained = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert source.status is SourceStatus.ERROR and source.indexed_count == 2
        assert source.last_error == result["error"]
        assert {d.id for d in retained} == {d.id for d in documents}
        assert all(d.status is DocumentStatus.READY for d in retained)
        chunks = (await session.execute(select(models.Chunk))).scalars().all()
        assert {
            c.id: (c.document_id, c.text, list(c.embedding), c.embedding_fingerprint)
            for c in chunks
        } == chunk_snapshot
    assert connector.sync_calls == 1
    assert _FakeIndexStore.events == index_operations
    assert store.objects == object_snapshot


async def test_reconciliation_archive_is_tenant_scoped_idempotent_and_atomic(
    source_db: bool,
) -> None:
    """R3-002: the archive cannot cross tenants, duplicate, or outlive rollback."""
    tenant_id, source_id = await _seed()
    fingerprint = get_settings().embedding_space_fingerprint
    async with db_session.tenant_session_scope(tenant_id) as session:
        source = await SourceRepository(session, tenant_id).get(source_id)
        assert source is not None
        document = await DocumentRepository(session, tenant_id).create(
            owner_id=source.owner_id,
            collection_id=UUID(str(source.config["collection_id"])),
            filename="rollback.txt",
            mime_type="text/plain",
            size_bytes=8,
            storage_key="r4/rollback",
            acl_enforced=False,
            source_id=source_id,
        )
        row = models.Chunk(
            tenant_id=tenant_id,
            document_id=document.id,
            ord=0,
            text="rollback",
            char_start=0,
            char_end=8,
            legacy_embedding=[0.25] * 1024,
        )
        session.add(row)
    with pytest.raises(OperationalError, match="abort reconciliation"):
        async with db_session.tenant_session_scope(tenant_id) as session:
            # RLS allows A here, so the B repository predicate is independently
            # load-bearing even before the database's tenant backstop.
            await ChunkRepository(session, uuid4()).archive_legacy_for_reconciliation(
                document.id, replacement_fingerprint=fingerprint
            )
            assert not (
                (await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all()
            )
            chunks = ChunkRepository(session, tenant_id)
            await chunks.archive_legacy_for_reconciliation(
                document.id, replacement_fingerprint=fingerprint
            )
            [first] = (await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all()
            await chunks.archive_legacy_for_reconciliation(
                document.id, replacement_fingerprint=fingerprint
            )
            [repeat] = (
                (await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all()
            )
            assert first.id == repeat.id and list(repeat.embedding) == [0.25] * 1024
            await DocumentRepository(session, tenant_id).delete(document.id)
            await session.flush()  # real cascade, then a failed reconcile transaction
            raise OperationalError("abort reconciliation", {}, Exception("abort reconciliation"))
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert await DocumentRepository(session, tenant_id).get(document.id) is not None
        retained = (await session.execute(select(models.Chunk))).scalar_one()
        assert list(retained.legacy_embedding) == [0.25] * 1024
        assert not ((await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all())


@pytest.mark.parametrize("source_type", ["web", "gdrive"])
@pytest.mark.parametrize("changed", [False, True], ids=["unchanged", "changed"])
@pytest.mark.parametrize("backfilled", [False, True], ids=["legacy-only", "backfilled"])
async def test_full_source_replay_preserves_exact_legacy_archive(
    source_db: bool,
    monkeypatch: pytest.MonkeyPatch,
    source_type: str,
    changed: bool,
    backfilled: bool,
) -> None:
    """R3-002: real full reconciliation may never cascade away rollback bytes."""
    tenant_id, source_id = await _seed(source_type)
    settings = get_settings().model_copy(
        update={"ingestion_chunk_size": 120, "ingestion_chunk_overlap": 20}
    )
    connector = FakeAclConnector()
    connector.name = source_type
    connector.full_result = FullSyncResult(
        docs=(_doc("d1", principals=["tenant"], text="retained rollback passage. " * 30),),
        baseline_cursor=None,  # managed source has no cursor: next run is FULL
    )
    if source_type == "web":
        # Web sources use owner ACLs, not the managed connector capability.
        monkeypatch.setattr(sync_module, "get_map_acl", lambda _: None)
    monkeypatch.setattr(sync_module, "get_connector", lambda _: connector)
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", _FakeIndexStore)
    store = _FakeObjectStore()

    async def _sync() -> object:
        return await sync_module.sync_source_async(
            tenant_id, source_id, settings=settings, object_store=store, gateway=_NativeGateway()
        )

    first = await _sync()
    assert first.status is SourceStatus.READY
    async with db_session.tenant_session_scope(tenant_id) as session:
        [old] = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        chunks = list(
            (await session.execute(select(models.Chunk).where(models.Chunk.document_id == old.id)))
            .scalars()
            .all()
        )
        assert len(chunks) > 1
        for row in chunks:
            row.legacy_embedding = [0.125 + row.ord / 128] * 1024
            if not backfilled:
                row.embedding = None
                row.embedding_fingerprint = None
        expected = {
            row.ord: (row.id, row.text, row.char_start, row.char_end, list(row.legacy_embedding))
            for row in chunks
        }
    payload = [[ord_, *values[1:4]] for ord_, values in sorted(expected.items())]
    revision = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    if changed:
        connector.full_result = FullSyncResult(
            docs=(_doc("d1", principals=["tenant"], text="new reshaped source content. " * 12),),
            baseline_cursor=None,
        )
    second = await _sync()
    assert second.status is SourceStatus.READY and second.indexed_count == 1
    assert connector.sync_calls == 2 and connector.fetch_calls == []
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert await DocumentRepository(session, tenant_id).get(old.id) is None
        archives = list(
            (await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all()
        )
        assert len(archives) == len(expected), "full replay lost the rollback vector set"
        assert {
            row.ord: (
                row.original_chunk_id,
                row.text,
                row.char_start,
                row.char_end,
                list(row.embedding),
            )
            for row in archives
        } == expected
        assert all(
            row.tenant_id == tenant_id
            and row.document_id == old.id
            and row.content_revision == revision
            and row.replacement_attempt == old.ingestion_attempts + 1
            and row.replacement_fingerprint == settings.embedding_space_fingerprint
            for row in archives
        )
        [current] = await DocumentRepository(session, tenant_id).list_for_source(source_id)
        assert current.status is DocumentStatus.READY
        native = await ChunkRepository(session, tenant_id).list_for_document(current.id)
        assert native and all(len(row.embedding) == 2048 for row in native)
        if source_db:
            # FORCE RLS hides another tenant's archive even without an explicit
            # SELECT predicate. A foreign repository also cannot archive it.
            await bind_tenant(session, uuid4())
            assert (
                not (await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all()
            )
    # Repeated full replay has no remaining legacy rows: no duplicate archive.
    assert (await _sync()).status is SourceStatus.READY
    async with db_session.tenant_session_scope(tenant_id) as session:
        assert len(
            (await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all()
        ) == len(expected)
    if source_db:
        with pytest.raises(DBAPIError, match="detached legacy archive populated"):
            await asyncio.to_thread(
                command.downgrade, _migration_config(), "0044_direct_media_uploads"
            )
        async with db_session.tenant_session_scope(tenant_id) as session:
            assert (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == ("0045_embedding_contract")
            retained = (
                (await session.execute(select(models.LegacyEmbeddingArchive))).scalars().all()
            )
            assert {row.ord: list(row.embedding) for row in retained} == {
                ord_: values[4] for ord_, values in expected.items()
            }
