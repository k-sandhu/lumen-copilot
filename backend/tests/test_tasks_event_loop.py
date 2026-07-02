"""Worker event-loop / engine-lifecycle regression tests (#140).

A Celery prefork worker drives each task's async core on a **fresh** event loop
(``asyncio.run`` per invocation). The process-wide async engine
(:mod:`app.db.session`) binds its pooled connections to the loop that first
opened them. If a connection pooled by one task run is reused by the next run —
on a different, now-closed loop — the production driver (``asyncpg``) raises
``RuntimeError: ... got Future ... attached to a different loop`` /
``Event loop is closed``. The failure path itself writes to the DB, so it *also*
crashes, stranding the document in ``processing`` forever (AC-6 violation).

**Faithful offline model.** The default offline driver ``aiosqlite`` *drops* its
connection when the loop closes, so a naive "run the task twice" test cannot
reproduce the bug. ``asyncpg`` instead (a) **keeps** the pooled connection across
the loop close and (b) **binds** it to its creating loop. We model both:

* ``StaticPool`` keeps the single connection alive across the ``asyncio.run``
  loops (like ``asyncpg``'s real pool, unlike ``aiosqlite``'s default);
* ``connect``/``checkout`` pool events capture the creating loop and raise — with
  ``asyncpg``'s exact message — when the connection is later used from a
  different running loop.

With that model, two sequential task invocations in one process crash on the 2nd
**without** the fix, and both succeed **with** it (the engine is disposed inside
each run's loop, so no pooled connection ever outlives its loop).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
import app.tasks.ingest as ingest_module
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.db.base import Base
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import Embedding
from app.tasks.ingest import ingest_document

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_DIM = 8


# --- Test settings ----------------------------------------------------------


def _settings(url: str, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": url,
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
        **overrides,
    }
    return Settings(**base)  # type: ignore[arg-type]


def _temp_db_url() -> tuple[str, str]:
    """A fresh file-backed SQLite URL + its temp dir (caller removes the dir).

    Uses ``mkdtemp`` (a uniquely-named ``os.mkdir``, no parent scan) rather than
    pytest's ``tmp_path`` so it works in sandboxes where the temp base cannot be
    enumerated. A **file** DB (not in-memory) so the seeded rows survive the
    per-run engine dispose+rebuild that the fix performs.
    """
    directory = tempfile.mkdtemp(prefix="lumen-loop-")
    url = f"sqlite+aiosqlite:///{Path(os.path.join(directory, 't.db')).as_posix()}"
    return url, directory


# --- The asyncpg loop-affinity model ----------------------------------------


class _DifferentLoopError(RuntimeError):
    """Stand-in for asyncpg's 'got Future ... attached to a different loop'."""


def _install_loop_affinity(sync_engine: Engine) -> None:
    """Make a pooled connection refuse use from a loop other than its creator's.

    Captures the running loop when the DBAPI connection is opened (``connect``)
    and, on every checkout, asserts the *current* running loop is that same loop —
    raising the asyncpg-style error otherwise. This is the one behaviour aiosqlite
    lacks that makes the cross-loop reuse bug observable offline.
    """

    @event.listens_for(sync_engine, "connect")
    def _capture(_dbapi_conn: object, record: object) -> None:
        record.info["loop"] = asyncio.get_running_loop()  # type: ignore[attr-defined]

    @event.listens_for(sync_engine, "checkout")
    def _verify(_dbapi_conn: object, record: object, _proxy: object) -> None:
        bound = record.info.get("loop")  # type: ignore[attr-defined]
        if bound is not None and bound is not asyncio.get_running_loop():
            raise _DifferentLoopError("got Future <Future pending> attached to a different loop")


def _affinity_engine_factory(url: str) -> Callable[..., AsyncEngine]:
    """A ``create_async_engine`` replacement: StaticPool + loop affinity.

    Installed over ``db.session.create_async_engine`` so every engine the real
    ``get_engine`` builds (including the one rebuilt after a per-run dispose) is
    loop-bound and connection-persistent, i.e. behaves like the asyncpg pool.
    """

    def _factory(*_args: object, **_kwargs: object) -> AsyncEngine:
        engine = create_async_engine(
            url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )
        _install_loop_affinity(engine.sync_engine)
        return engine

    return _factory


async def _dispose_db_session() -> None:
    """Best-effort dispose of whatever engine ``db.session`` currently holds."""
    engine = db_session._engine
    if engine is not None:
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            pass


# --- Fakes (mirror test_ingestion_task) -------------------------------------


class _FakeObjectStore:
    """In-memory object store keyed like the real adapter (tenant-prefixed)."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def put(self, tenant_id: str, key: str, data: bytes) -> None:
        self._objects[(tenant_id, key)] = data

    async def get(self, tenant_id: str, key: str) -> bytes:
        try:
            return self._objects[(tenant_id, key)]
        except KeyError as exc:
            raise NotFoundError("object not found", code="object_not_found") from exc


class _FakeGateway:
    """Fake LLM gateway: a deterministic vector per input (offline)."""

    async def embed(self, inputs: Sequence[str], *, model: str | None = None) -> list[Embedding]:
        return [Embedding(vector=[float(len(t) % 7)] * _DIM, model="fake") for t in inputs]


class _FakeRequest:
    def __init__(self, retries: int) -> None:
        self.retries = retries


class _FakeTask:
    """A fake Celery task ``self`` — the success path reads only ``request``."""

    def __init__(self, retries: int = 0) -> None:
        self.request = _FakeRequest(retries)


class _FakeIndexStore:
    """Offline stand-in for ``app.search.OpenSearchStore`` in the index-sync core.

    Ingestion dual-writes to the search index (ADR-0010 §5); patching the store
    class keeps this event-loop test offline. Write-path behaviour is asserted
    in ``test_index_sync.py``.
    """

    @classmethod
    def from_settings(cls, settings: object) -> _FakeIndexStore:
        return cls()

    async def ensure_index(self) -> None:
        return None

    async def upsert_chunks(self, chunks: Sequence[object], *, refresh: bool = False) -> None:
        return None

    async def delete_document(
        self, *, tenant_id: uuid.UUID, document_id: uuid.UUID, refresh: bool = False
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _offline_index_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the index-sync core at the fake store — no engine, tests stay offline."""
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", _FakeIndexStore)


# --- Seeding (separate engine, no affinity — runs on the setup loop) ---------


async def _setup_and_seed(url: str, *, count: int) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """Create the schema and seed ``count`` pending text documents in the file DB.

    Uses its **own** default-pool engine (no loop affinity) and disposes it before
    the task runs, so the StaticPool connection the task will reuse is first opened
    *inside the task's loop* — not pinned to this setup loop.
    """
    setup_engine = create_async_engine(url)
    async with setup_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(bind=setup_engine, expire_on_commit=False)
    seeded: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    async with maker() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        user = await UserRepository(session, tenant.id).create(
            email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
        )
        coll = await CollectionRepository(session, tenant.id).create(owner_id=user.id, name="c")
        for n in range(count):
            doc = await DocumentRepository(session, tenant.id).create(
                owner_id=user.id,
                collection_id=coll.id,
                filename=f"f{n}.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key=f"key-{n}",
            )
            seeded.append((tenant.id, doc.id, doc.storage_key))
        await session.commit()
    await setup_engine.dispose()
    return seeded


# --- The regression: two task invocations in one warm process ---------------


def test_two_sequential_ingest_invocations_in_one_process_both_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm worker ingests two documents in a row without a cross-loop crash.

    Drives the real Celery entrypoint ``ingest_document`` (its unwrapped function
    with a fake ``self``) twice — each on its own ``asyncio.run`` loop, exactly as
    the worker does. Against the asyncpg loop-affinity model the **2nd** invocation
    reuses the 1st run's pooled connection on a closed loop and dies *without* the
    per-run dispose fix; with it, both end ``ready``.
    """
    url, directory = _temp_db_url()
    (tid_a, did_a, key_a), (tid_b, did_b, key_b) = asyncio.run(_setup_and_seed(url, count=2))

    # Model the production driver: pooled connection is loop-bound + persistent.
    monkeypatch.setattr(db_session, "create_async_engine", _affinity_engine_factory(url))
    db_session._engine = None
    db_session._sessionmaker = None

    settings = _settings(url)
    store = _FakeObjectStore()
    store.put(str(tid_a), key_a, ("alpha beta gamma delta. " * 30).encode())
    store.put(str(tid_b), key_b, ("epsilon zeta eta theta. " * 30).encode())
    gateway = _FakeGateway()
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ingest_module, "ObjectStore", lambda _s: store)
    monkeypatch.setattr(ingest_module, "LLMGateway", lambda _s: gateway)

    wrapper = ingest_document.__wrapped__.__func__  # the raw fn under the task

    try:
        first = wrapper(_FakeTask(0), str(tid_a), str(did_a))
        # The 2nd invocation on a warm worker: crashes pre-fix (loop reuse).
        second = wrapper(_FakeTask(0), str(tid_b), str(did_b))

        assert first["status"] == DocumentStatus.READY.value
        assert first["chunk_count"] > 0
        assert second["status"] == DocumentStatus.READY.value
        assert second["chunk_count"] > 0
    finally:
        asyncio.run(_dispose_db_session())
        db_session._engine = None
        db_session._sessionmaker = None
        shutil.rmtree(directory, ignore_errors=True)


def test_run_task_disposes_engine_after_each_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix's contract: ``run_task`` disposes the engine inside the run's loop.

    After each ``run_task`` the process-wide engine is gone (``_engine is None``),
    so the next task builds a fresh engine bound to its own loop — no pooled
    connection ever outlives the loop that created it. Two runs in a row both
    succeed under the same asyncpg loop-affinity model.
    """
    from app.tasks.runner import run_task

    url, directory = _temp_db_url()
    monkeypatch.setattr(db_session, "create_async_engine", _affinity_engine_factory(url))
    db_session._engine = None
    db_session._sessionmaker = None

    async def _probe() -> str:
        async with db_session.session_scope() as session:
            await session.execute(text("SELECT 1"))
        return "ok"

    try:
        assert run_task(_probe()) == "ok"
        assert db_session._engine is None  # disposed inside the run's own loop
        # A second run starts clean — would reuse a dead-loop connection if the
        # engine had survived, tripping the affinity check.
        assert run_task(_probe()) == "ok"
        assert db_session._engine is None
    finally:
        asyncio.run(_dispose_db_session())
        db_session._engine = None
        db_session._sessionmaker = None
        shutil.rmtree(directory, ignore_errors=True)
