"""Ingestion task tests — the async pipeline core (CC-5 #21, AC-1..AC-6).

Exercises :func:`app.tasks.ingest.ingest_document_async` end-to-end against an
in-memory SQLite database (the portable column types make ``chunks`` work without
Postgres), a **fake object store**, and a **fake LLM gateway** so the suite is
offline (no MinIO, no model key — AC: embedding-dependent tests fake ``embed()``).

Coverage:

* AC-1..AC-3: a stored text document is fetched, parsed, chunked, embedded
  (batched), and chunks persist with vector + offsets + tenant + document + ord;
* AC-5: status advances ``pending → processing → ready`` with ``chunk_count``;
  a **re-run replaces** the chunks (idempotent, no duplicates);
* AC-6 (negative): a corrupt/unsupported document ends ``status=failed`` with a
  recorded reason (not a crash, not a silent drop);
* embeddings are **batched** (the fake counts its calls);
* INV-1: chunks persist under the document's tenant only;
* transient faults (storage/model unavailable) raise the retryable error.

The Celery **sync wrapper** (``ingest_document``) and the **enqueue seam**
(``enqueue_ingestion``) are covered separately at the bottom of the file with a
fake Celery ``self`` and a faked broker connection, so the retry/backoff,
dead-letter, and broker-outage-swallow branches are exercised fully offline (no
real broker, no datastore).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
import pytest_asyncio
from celery.exceptions import Retry
from kombu.exceptions import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.session as db_session
import app.tasks.ingest as ingest_module
from app.core.config import Settings
from app.core.errors import DependencyError, NotFoundError
from app.db.base import Base
from app.db.repositories import (
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import Embedding
from app.tasks.celery_app import celery_app
from app.tasks.ingest import (
    IngestionError,
    IngestionResult,
    enqueue_ingestion,
    ingest_document,
    ingest_document_async,
)

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip

_DIM = 8  # tiny embedding width for the tests (gateway is faked)


# --- Test settings ----------------------------------------------------------


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
        **overrides,
    }
    return Settings(**base)  # type: ignore[arg-type]


# --- Fakes ------------------------------------------------------------------


class _FakeObjectStore:
    """In-memory object store keyed like the real adapter (tenant-prefixed)."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}
        self.fail_with: Exception | None = None

    def put(self, tenant_id: str, key: str, data: bytes) -> None:
        self._objects[(tenant_id, key)] = data

    async def get(self, tenant_id: str, key: str) -> bytes:
        if self.fail_with is not None:
            raise self.fail_with
        try:
            return self._objects[(tenant_id, key)]
        except KeyError as exc:
            raise NotFoundError("object not found", code="object_not_found") from exc


class _FakeGateway:
    """Fake LLM gateway: returns a deterministic vector per input, counts batches."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[int] = []  # batch sizes, in order

    async def embed(self, inputs: Sequence[str], *, model: str | None = None) -> list[Embedding]:
        if self.fail:
            raise DependencyError("no key", code="llm_unconfigured")
        self.calls.append(len(inputs))
        return [Embedding(vector=[float(len(t) % 7)] * _DIM, model="fake") for t in inputs]


class _FakeIndexStore:
    """Offline stand-in for ``app.search.OpenSearchStore`` in the index-sync core.

    Ingestion dual-writes to the search index after persisting chunks
    (ADR-0010 §5); patching the store class keeps that write offline. The
    write-path behaviour itself is asserted in ``test_index_sync.py``.
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


# --- SQLite-backed session_scope override -----------------------------------


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[None]:
    """Point ``db.session`` globals at a fresh in-memory SQLite for the task.

    The task calls ``session_scope()`` (which uses the module-global sessionmaker),
    so we install a SQLite-backed engine/sessionmaker on the module for the test
    and restore the originals afterwards. ``StaticPool`` keeps the single
    in-memory connection alive across ``session_scope`` checkouts.
    """
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
    db_session._sessionmaker = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield
    finally:
        db_session._engine = prev_engine
        db_session._sessionmaker = prev_maker
        await engine.dispose()


async def _seed_document(*, mime_type: str, key: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Create tenant/user/collection/document (status=pending); return ids."""
    async with db_session.session_scope() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        user = await UserRepository(session, tenant.id).create(
            email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
        )
        coll = await CollectionRepository(session, tenant.id).create(owner_id=user.id, name="c")
        doc = await DocumentRepository(session, tenant.id).create(
            owner_id=user.id,
            collection_id=coll.id,
            filename="f",
            mime_type=mime_type,
            size_bytes=1,
            storage_key=key,
        )
    return tenant.id, doc.id


# --- Happy path (AC-1..AC-3, AC-5) ------------------------------------------


async def test_ingest_text_document_persists_chunks_and_marks_ready(
    sqlite_engine: None,
) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    body = (
        "The quick brown fox jumps over the lazy dog. " * 10
        + "Pack my box with five dozen liquor jugs. " * 10
    ).encode("utf-8")
    key = "k-text"
    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="placeholder")
    # Store under the real storage_key the seeded document carries.
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, body)

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )

    assert result.status is DocumentStatus.READY
    assert result.chunk_count > 1

    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        assert doc.status is DocumentStatus.READY
        assert doc.error is None

        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        assert len(chunks) == result.chunk_count
        # AC-2/AC-3: each chunk has offsets, an embedding of the right width, the
        # tenant + document scope, and contiguous ordinals.
        text = body.decode()
        for i, c in enumerate(chunks):
            assert c.ord == i
            assert c.tenant_id == tenant_id
            assert c.document_id == document_id
            assert c.embedding is not None and len(c.embedding) == _DIM
            assert text[c.char_start : c.char_end] == c.text


async def test_embeddings_are_batched(sqlite_engine: None) -> None:
    """The gateway is called once per batch, not once per chunk (batched)."""
    settings = _settings(INGESTION_EMBED_BATCH_SIZE="3")
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="x")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, ("sentence number x. " * 60).encode())

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )
    n = result.chunk_count
    assert n > 3  # enough chunks that batching is observable
    # Number of embed() calls == ceil(n / batch_size), each <= batch_size.
    assert len(gateway.calls) == (n + 2) // 3
    assert all(size <= 3 for size in gateway.calls)
    assert sum(gateway.calls) == n


async def test_reingest_replaces_chunks_idempotent(sqlite_engine: None) -> None:
    """AC-5: re-running ingestion replaces, never duplicates, a document's chunks."""
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="y")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, ("alpha beta gamma delta. " * 40).encode())

    first = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    second = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )

    assert first.chunk_count == second.chunk_count
    async with db_session.session_scope() as session:
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        # Replaced, not duplicated.
        assert len(chunks) == second.chunk_count
        # Ordinals still contiguous after the replace.
        assert [c.ord for c in chunks] == list(range(len(chunks)))


# --- Negatives (AC-6) -------------------------------------------------------


async def test_unsupported_type_marks_failed(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="image/png", key="z")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, b"\x89PNG not an ingestible type")

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.FAILED
    assert result.error  # a recorded reason, not None

    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        assert doc.status is DocumentStatus.FAILED
        assert doc.error


async def test_corrupt_pdf_marks_failed_not_crash(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="application/pdf", key="p")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, b"%PDF-1.4 broken")

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.FAILED
    assert "PDF" in (result.error or "")


async def test_empty_document_is_ready_with_zero_chunks(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="e")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, b"   \n  ")  # whitespace only

    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.READY
    assert result.chunk_count == 0
    # And no chunks were left behind / created.
    async with db_session.session_scope() as session:
        chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
        assert chunks == []


async def test_missing_document_is_noop(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway()
    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    result = await ingest_document_async(
        tenant_id,
        document_id,
        settings=settings,
        object_store=store,
        gateway=gateway,  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.FAILED
    assert "not found" in (result.error or "")


# --- Transient faults are retryable (raised, not swallowed) -----------------


async def test_storage_unavailable_raises_retryable(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    store.fail_with = DependencyError("minio down", code="dependency_unavailable")
    gateway = _FakeGateway()

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="s")
    with pytest.raises(IngestionError):
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=settings,
            object_store=store,
            gateway=gateway,  # type: ignore[arg-type]
        )


async def test_embed_unavailable_raises_retryable(sqlite_engine: None) -> None:
    settings = _settings()
    store = _FakeObjectStore()
    gateway = _FakeGateway(fail=True)

    tenant_id, document_id = await _seed_document(mime_type="text/plain", key="m")
    async with db_session.session_scope() as session:
        doc = await DocumentRepository(session, tenant_id).get(document_id)
        assert doc is not None
        key = doc.storage_key
    store.put(str(tenant_id), key, ("text to embed. " * 30).encode())

    with pytest.raises(IngestionError):
        await ingest_document_async(
            tenant_id,
            document_id,
            settings=settings,
            object_store=store,
            gateway=gateway,  # type: ignore[arg-type]
        )


# --- The Celery sync wrapper: retry / backoff / dead-letter -----------------
#
# ``ingest_document`` (the ``@celery_app.task`` entrypoint) is the seam that maps
# the async core's outcomes onto Celery's retry/terminal semantics. We drive its
# *unwrapped* function with a fake ``self`` (a stand-in task object exposing
# ``request.retries`` and a ``retry(...)`` that raises Celery's ``Retry`` exactly
# as the real one does), and stub the heavy collaborators it builds
# (``get_settings`` / ``ObjectStore`` / ``LLMGateway``) plus the async core so the
# branch under test runs fully offline — no broker, no datastore, no event loop
# into the real pipeline.

# The raw (unbound) function under the Celery task wrapper. ``bind=True`` makes
# Celery bind ``self`` to the task singleton on ``__wrapped__``; ``.__func__``
# peels that off so we can drive it with our own fake ``self`` (``_FakeTask``).
_ingest_wrapper = ingest_document.__wrapped__.__func__


class _FakeRequest:
    """Stand-in for ``self.request`` — only ``retries`` is read by the wrapper."""

    def __init__(self, retries: int) -> None:
        self.retries = retries


class _FakeTask:
    """A fake Celery task ``self``: exposes ``request`` and a ``retry`` that raises.

    Mirrors ``celery.app.task.Task.retry`` closely enough for the wrapper: it
    records the ``exc``/``countdown`` it was called with, then raises Celery's
    real :class:`~celery.exceptions.Retry` (which is what the production code
    re-raises). The test asserts on both the raised type and the recorded
    ``countdown`` so the exponential-backoff math is pinned.
    """

    def __init__(self, retries: int) -> None:
        self.request = _FakeRequest(retries)
        self.retry_calls: list[dict[str, object]] = []

    def retry(self, *, exc: BaseException, countdown: float) -> Retry:
        self.retry_calls.append({"exc": exc, "countdown": countdown})
        raise Retry("retrying", exc=exc, when=countdown)


def _patch_wrapper_collaborators(monkeypatch: pytest.MonkeyPatch, *, settings: Settings) -> None:
    """Stub the heavy collaborators the wrapper constructs from settings.

    The wrapper resolves config via ``get_settings`` and builds an
    ``ObjectStore`` / ``LLMGateway`` before delegating to the async core; for the
    retry/dead-letter branches none of those need to be real, so we hand back the
    test ``settings`` and inert adapters. The async core itself is patched
    per-test to inject the fault (or success) under exercise.
    """
    monkeypatch.setattr(ingest_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ingest_module, "ObjectStore", lambda _s: object())
    monkeypatch.setattr(ingest_module, "LLMGateway", lambda _s: object())


def test_wrapper_transient_fault_retries_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient fault on a non-final attempt → Celery ``Retry`` with backoff.

    The countdown is ``ingestion_retry_backoff_seconds * 2**retries``; we assert
    it for two distinct (non-final) attempt counts so the *exponential* shape —
    not just "some delay" — is pinned. Also exercises the UUID-string parsing in
    the wrapper (real UUID strings are passed in).
    """
    settings = _settings(INGESTION_MAX_RETRIES="3", INGESTION_RETRY_BACKOFF_SECONDS="5")
    _patch_wrapper_collaborators(monkeypatch, settings=settings)

    # Async core raises a retryable fault; the wrapper must translate to Retry.
    def _boom(*_a: object, **_k: object) -> object:
        raise IngestionError("storage flaked")

    monkeypatch.setattr(ingest_module, "ingest_document_async", _boom)

    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()

    # Attempt 0 → countdown 5 * 2**0 == 5.
    task0 = _FakeTask(retries=0)
    with pytest.raises(Retry):
        _ingest_wrapper(task0, str(tenant_id), str(document_id))
    assert len(task0.retry_calls) == 1
    assert task0.retry_calls[0]["countdown"] == 5
    assert isinstance(task0.retry_calls[0]["exc"], IngestionError)

    # Attempt 2 → countdown 5 * 2**2 == 20 (exponential, not linear).
    task2 = _FakeTask(retries=2)
    with pytest.raises(Retry):
        _ingest_wrapper(task2, str(tenant_id), str(document_id))
    assert task2.retry_calls[0]["countdown"] == 20


def test_wrapper_dependency_error_also_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``DependencyError`` from the core is retried just like ``IngestionError``."""
    settings = _settings(INGESTION_MAX_RETRIES="3", INGESTION_RETRY_BACKOFF_SECONDS="2")
    _patch_wrapper_collaborators(monkeypatch, settings=settings)

    def _boom(*_a: object, **_k: object) -> object:
        raise DependencyError("model down", code="llm_unconfigured")

    monkeypatch.setattr(ingest_module, "ingest_document_async", _boom)

    task = _FakeTask(retries=1)
    with pytest.raises(Retry):
        _ingest_wrapper(task, str(uuid.uuid4()), str(uuid.uuid4()))
    # base 2 * 2**1 == 4.
    assert task.retry_calls[0]["countdown"] == 4
    assert isinstance(task.retry_calls[0]["exc"], DependencyError)


def test_wrapper_final_attempt_dead_letters_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the final attempt the doc is marked ``failed`` and the task RETURNS.

    ``request.retries >= ingestion_max_retries`` is the dead-letter boundary: the
    wrapper must NOT raise ``Retry`` (that would redeliver forever) — it records a
    permanent ``failed`` status via ``_fail`` and returns the result dict so the
    message is acknowledged. We fake ``_fail`` (no datastore) and assert it was
    called with the parsed UUIDs and a reason mentioning the retry count, and that
    the returned dict reports ``status=failed`` and ``chunk_count=0``.
    """
    settings = _settings(INGESTION_MAX_RETRIES="3", INGESTION_RETRY_BACKOFF_SECONDS="5")
    _patch_wrapper_collaborators(monkeypatch, settings=settings)

    def _boom(*_a: object, **_k: object) -> object:
        raise IngestionError("storage still down")

    monkeypatch.setattr(ingest_module, "ingest_document_async", _boom)

    fail_calls: list[tuple[uuid.UUID, uuid.UUID, str]] = []

    async def _fake_fail(tid: uuid.UUID, did: uuid.UUID, reason: str) -> IngestionResult:
        fail_calls.append((tid, did, reason))
        return IngestionResult(did, DocumentStatus.FAILED, 0, reason)

    monkeypatch.setattr(ingest_module, "_fail", _fake_fail)

    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    task = _FakeTask(retries=3)  # retries >= max_retries → dead-letter

    # Returns (does not raise): no infinite redelivery.
    result = _ingest_wrapper(task, str(tenant_id), str(document_id))

    assert task.retry_calls == []  # never asked Celery to retry
    assert result["status"] == DocumentStatus.FAILED.value
    assert result["chunk_count"] == 0
    # ``_fail`` got the parsed UUIDs (not the raw strings) and a reason that
    # records the exhausted-retry count.
    assert len(fail_calls) == 1
    tid, did, reason = fail_calls[0]
    assert (tid, did) == (tenant_id, document_id)
    assert "after 3 retries" in reason


def test_wrapper_success_returns_result_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Success path: the wrapper renders the core's result as the JSON-able dict.

    Also pins the UUID-string → ``UUID`` parsing: the async core is handed parsed
    ``UUID`` objects, never the raw strings Celery serialized.
    """
    settings = _settings()
    _patch_wrapper_collaborators(monkeypatch, settings=settings)

    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    seen_args: dict[str, object] = {}

    async def _ok(tid: uuid.UUID, did: uuid.UUID, **_k: object) -> IngestionResult:
        seen_args["tid"] = tid
        seen_args["did"] = did
        return IngestionResult(did, DocumentStatus.READY, 7)

    monkeypatch.setattr(ingest_module, "ingest_document_async", _ok)

    task = _FakeTask(retries=0)
    result = _ingest_wrapper(task, str(tenant_id), str(document_id))

    assert task.retry_calls == []
    assert result == {
        "document_id": str(document_id),
        "status": DocumentStatus.READY.value,
        "chunk_count": 7,
        "error": None,
    }
    # UUID-string parsing: the core received parsed UUIDs of the right value/type.
    assert seen_args["tid"] == tenant_id and isinstance(seen_args["tid"], uuid.UUID)
    assert seen_args["did"] == document_id and isinstance(seen_args["did"], uuid.UUID)


# --- The enqueue seam: bounded reconnect + best-effort swallow --------------
#
# ``enqueue_ingestion`` runs after-commit on the request path; a broker outage
# must never 500 a successful upload, so an ``OperationalError`` from the bounded
# reconnect is logged and swallowed (the doc stays ``pending``). We fake the
# broker connection so nothing real is published.


class _FakeConnection:
    """A fake broker connection usable as a context manager.

    ``ensure_connection`` is where a real bounded reconnect would raise on an
    unreachable broker; the test sets ``raise_on_ensure`` to simulate the outage.
    """

    def __init__(self, *, raise_on_ensure: Exception | None = None) -> None:
        self.raise_on_ensure = raise_on_ensure
        self.ensure_kwargs: dict[str, object] = {}

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def ensure_connection(self, **kwargs: object) -> None:
        self.ensure_kwargs = kwargs
        if self.raise_on_ensure is not None:
            raise self.raise_on_ensure


def test_enqueue_swallows_broker_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker ``OperationalError`` is logged and swallowed — no raise.

    A transient broker outage after a committed upload must not turn the request
    into a 500; the document is simply left ``pending``. We make the bounded
    reconnect raise ``kombu.exceptions.OperationalError`` and assert
    ``enqueue_ingestion`` returns normally and never publishes.
    """
    conn = _FakeConnection(raise_on_ensure=OperationalError("broker unreachable"))
    monkeypatch.setattr(celery_app, "connection_for_write", lambda: conn)

    published: list[object] = []
    monkeypatch.setattr(
        ingest_module.ingest_document,
        "apply_async",
        lambda *a, **k: published.append((a, k)),
    )

    # Must not raise.
    enqueue_ingestion(uuid.uuid4(), uuid.uuid4())

    # The bounded-reconnect cap was actually requested, and nothing was published.
    assert conn.ensure_kwargs == {"max_retries": 1, "timeout": 2}
    assert published == []


def test_enqueue_publishes_once_with_string_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive path: ``apply_async`` is called exactly once with string id args.

    Celery's JSON serializer can't carry ``UUID``, so the ids must be passed as
    strings; the publish goes on the bounded connection with ``retry=False``.
    """
    conn = _FakeConnection()
    monkeypatch.setattr(celery_app, "connection_for_write", lambda: conn)

    calls: list[dict[str, object]] = []

    def _apply_async(*_args: object, **kwargs: object) -> None:
        calls.append(dict(kwargs))

    monkeypatch.setattr(ingest_module.ingest_document, "apply_async", _apply_async)

    tenant_id = uuid.uuid4()
    document_id = uuid.uuid4()
    enqueue_ingestion(tenant_id, document_id)

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["args"] == (str(tenant_id), str(document_id))
    assert kwargs["connection"] is conn
    assert kwargs["retry"] is False
