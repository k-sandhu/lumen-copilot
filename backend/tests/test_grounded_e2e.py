"""End-to-end grounded-chat integration — the full loop, offline + live (#29).

The single test that proves the whole #29 chain wired together, not just each
link in isolation: **upload → ingest (parse→chunk→embed) → retrieve
(permission-filtered) → chat answer (agentic, streamed over WS) → citations
persisted + resolvable via GET .../messages**, plus the permission boundary
(INV-2/INV-3) — a second user in the same tenant cannot retrieve or be cited the
first user's passages.

Two layers, mirroring the established offline/live split:

* **Offline (CI-safe)** — a fake object store + fake-embedding gateway + the real
  ingestion task core (:func:`app.tasks.ingest.ingest_document_async`), the real
  chunker (#21), the deterministic permission-filtered retrieval, the real chat
  runtime over an in-memory backplane, all on in-memory SQLite. The
  ``app.db.session`` sessionmaker is pointed at the test DB so the *real* task
  code runs against it. Asserts the full wiring incl. the INV-2 boundary.
* **Live (key + stack)** — covered by ``tests/eval/test_eval_live.py`` (the real
  ingest + pgvector + model over the golden corpus); a thin live marker here
  documents that and skips cleanly so this module is self-describing.

The offline path is the headline: it exercises the genuine ingestion pipeline and
the genuine grounding/citation chokepoint, faking only the network edges
(storage + model), so a regression in the real loop fails here.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import Embedding, StreamEvent, ToolCall
from app.realtime.backplane import InMemoryBackplane
from app.services.chat_runtime import ChatRuntime
from app.tasks.ingest import ingest_document_async
from tests.eval.support import DeterministicEmbedder, OfflineRetrieval

import app.db.models  # noqa: F401  isort: skip

_MODEL = "anthropic/claude-opus-4.8"

_TAX_TEXT = (
    "US Federal Income Tax Guide for 2024.\n\n"
    "The standard deduction for a single filer in 2024 is $14,600. "
    "For married couples filing jointly, the standard deduction is $29,200.\n"
)

_BASE_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
    "REDIS_URL": "redis://localhost:6379/0",
    "CELERY_BROKER_URL": "redis://localhost:6379/1",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/2",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "test",
    "S3_SECRET_KEY": "test_secret",
    "S3_BUCKET": "test-bucket",
    "OPENROUTER_API_KEY": "sk-test",
    "INGESTION_CHUNK_SIZE": "200",
    "INGESTION_CHUNK_OVERLAP": "40",
}


def _settings() -> Settings:
    return Settings(**_BASE_ENV)  # type: ignore[arg-type]


# --- Fakes for the network edges (storage + model) --------------------------


class _FakeIndexStore:
    """Offline stand-in for ``app.search.OpenSearchStore`` in the index-sync core.

    Ingestion dual-writes to the search index (ADR-0010 §5); patching the store
    class keeps this end-to-end offline. Write-path behaviour is asserted in
    ``test_index_sync.py``; the live engine round-trip in ``test_search_store.py``.
    """

    @classmethod
    def from_settings(cls, settings: object) -> _FakeIndexStore:
        return cls()

    async def ensure_index(self) -> None:
        return None

    async def upsert_chunks(self, chunks: object, *, refresh: bool = False) -> None:
        return None

    async def delete_document(
        self, *, tenant_id: object, document_id: object, refresh: bool = False
    ) -> None:
        return None

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _offline_index_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the index-sync core at the fake store — no engine, tests stay offline."""
    monkeypatch.setattr("app.tasks.index_sync.OpenSearchStore", _FakeIndexStore)


class _FakeObjectStore:
    """An in-memory object store keyed by ``(tenant, key)`` — no MinIO needed."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def seed(self, tenant_id: str, key: str, data: bytes) -> None:
        self._objects[(tenant_id, key)] = data

    async def get(self, tenant_id: str, key: str) -> bytes:
        return self._objects[(tenant_id, key)]


class _FakeEmbeddingGateway:
    """A gateway exposing only ``embed`` (the ingestion path needs nothing else)."""

    def __init__(self, embedder: DeterministicEmbedder) -> None:
        self._embedder = embedder

    async def embed(self, inputs: list[str]) -> list[Embedding]:
        return await self._embedder.embed(inputs)


class _GroundedAnswerGateway:
    """Search-then-ground scripted gateway for the answer turn (offline).

    Turn 1 searches with the question; turn 2 grounds the answer in what the tool
    returned — and gives the honest "couldn't find it" when the tool found
    nothing, so the unanswerable / no-permission cases produce a faithful
    zero-citation refusal rather than a fabricated claim.
    """

    async def stream_tools(
        self, messages: object, *, tools: object, model: object = None, tool_choice: object = None
    ) -> AsyncIterator[StreamEvent]:
        msgs = list(messages)  # type: ignore[arg-type]
        tool_texts = [
            getattr(m, "content", "")
            for m in msgs
            if getattr(getattr(m, "role", None), "value", "") == "tool"
        ]
        if not tool_texts:
            question = next(
                (
                    getattr(m, "content", "")
                    for m in reversed(msgs)
                    if getattr(getattr(m, "role", None), "value", "") == "user"
                ),
                "",
            )
            yield StreamEvent(
                tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": question}),),
                finish_reason="tool_calls",
            )
            return
        joined = "\n".join(tool_texts)
        if "No matching passages" in joined or not joined.strip():
            yield StreamEvent(text="I couldn't find anything in your sources that answers that.")
            yield StreamEvent(finish_reason="stop")
            return
        yield StreamEvent(text="The 2024 single-filer standard deduction is $14,600.")
        yield StreamEvent(finish_reason="stop")


# --- Fixture: in-memory DB with two users in one tenant ---------------------


class _Ctx:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        retrieval_session: AsyncSession,
        tenant_id: uuid.UUID,
        alice: Principal,
        bob: Principal,
        store: _FakeObjectStore,
        embedder: DeterministicEmbedder,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.retrieval_session = retrieval_session
        self.tenant_id = tenant_id
        self.alice = alice
        self.bob = bob
        self.store = store
        self.embedder = embedder


@pytest_asyncio.fixture
async def ctx(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Ctx]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        # Point the real ingestion task's session_scope at this test DB so the
        # genuine task core runs against it (no Postgres).
        import app.db.session as db_session

        monkeypatch.setattr(db_session, "_sessionmaker", factory)
        monkeypatch.setattr(db_session, "_engine", engine)

        store = _FakeObjectStore()
        embedder = DeterministicEmbedder()
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="Acme")
            alice = await UserRepository(seed, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, tenant.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            await seed.commit()
            ctx = _Ctx(
                sessionmaker=factory,
                retrieval_session=seed,  # replaced below with a managed session
                tenant_id=tenant.id,
                alice=Principal(user_id=alice.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
                bob=Principal(user_id=bob.id, tenant_id=tenant.id, roles=(Role.MEMBER,)),
                store=store,
                embedder=embedder,
            )
        async with factory() as retrieval_session:
            ctx.retrieval_session = retrieval_session
            yield ctx
    finally:
        await engine.dispose()


async def _upload_and_ingest(ctx: _Ctx, *, owner: Principal, filename: str, text: str) -> uuid.UUID:
    """Register a pending document + stored bytes, then run the REAL ingest core.

    Mirrors what ``DocumentService.upload`` leaves behind (a ``pending`` row +
    object bytes) and then drives :func:`ingest_document_async` — the genuine
    parse→chunk→embed→persist pipeline — against the test DB, returning the
    document id once it is ``ready`` with chunks.
    """
    storage_key = f"{ctx.tenant_id}/{filename}"
    ctx.store.seed(str(ctx.tenant_id), storage_key, text.encode("utf-8"))
    async with ctx.sessionmaker() as session:
        coll = await CollectionRepository(session, ctx.tenant_id).create(
            owner_id=owner.user_id, name="c"
        )
        doc = await DocumentRepository(session, ctx.tenant_id).create(
            owner_id=owner.user_id,
            collection_id=coll.id,
            filename=filename,
            mime_type="text/plain",
            size_bytes=len(text.encode("utf-8")),
            storage_key=storage_key,
            status=DocumentStatus.PENDING,
        )
        await session.commit()
        document_id = doc.id

    result = await ingest_document_async(
        ctx.tenant_id,
        document_id,
        settings=_settings(),
        object_store=ctx.store,  # type: ignore[arg-type]
        gateway=_FakeEmbeddingGateway(ctx.embedder),  # type: ignore[arg-type]
    )
    assert result.status is DocumentStatus.READY, result.error
    assert result.chunk_count > 0
    return document_id


async def _ask(ctx: _Ctx, *, asker: Principal, question: str) -> tuple[uuid.UUID, list[str]]:
    """Run the full answer turn for ``asker`` and return (session_id, env types)."""
    async with ctx.sessionmaker() as session:
        chat = await ChatSessionRepository(session, ctx.tenant_id).create(
            owner_id=asker.user_id, model=_MODEL, title="s"
        )
        await session.commit()
        session_id = chat.id

    backplane = InMemoryBackplane()
    stream_id = uuid.uuid4().hex
    runtime = ChatRuntime(
        sessionmaker=ctx.sessionmaker,
        gateway=_GroundedAnswerGateway(),  # type: ignore[arg-type]
        backplane=backplane,
        principal=asker,
        request_id="e2e",
        source_ip="127.0.0.1",
        retrieval_factory=lambda _s: OfflineRetrieval(  # type: ignore[arg-type,return-value]
            ctx.retrieval_session, embedder=DeterministicEmbedder()
        ),
    )
    await runtime.run(
        stream_id=stream_id,
        session_id=session_id,
        question=question,
        model=_MODEL,
        history=[],
        collection_ids=None,
    )
    env_types = [
        str(e.get("type"))
        for e in backplane._replay.get(stream_id, ())  # noqa: SLF001
    ]
    return session_id, env_types


# --- The headline: full loop, offline ---------------------------------------


async def test_full_loop_upload_ingest_retrieve_chat_cite(ctx: _Ctx) -> None:
    """upload → ingest → retrieve → chat → citation persisted + resolvable."""
    document_id = await _upload_and_ingest(
        ctx, owner=ctx.alice, filename="tax-guide.txt", text=_TAX_TEXT
    )

    session_id, env_types = await _ask(
        ctx, asker=ctx.alice, question="What is the 2024 standard deduction for a single filer?"
    )
    # The WS lifecycle ran to a single terminal done, with a citation event.
    assert env_types[0] == "start"
    assert env_types[-1] == "done"
    assert env_types.count("done") == 1 and "error" not in env_types

    # The assistant message + its citation persisted and reload via the repos
    # (the GET .../messages path), resolving to alice's ingested document (CC-11).
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(session_id)
        assistant = [m for m in messages if m.role.value == "assistant"]
        assert len(assistant) == 1
        assert "14,600" in assistant[0].content
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message_hydrated(
            assistant[0].id
        )
    assert len(citations) >= 1
    cited_docs = {c.document_id for c in citations}
    assert document_id in cited_docs
    # The citation deep-links: its char span resolves back into the source text.
    cit = next(c for c in citations if c.document_id == document_id)
    assert _TAX_TEXT[cit.char_start : cit.char_end] == cit.snippet


async def test_permission_boundary_other_user_gets_no_citation(ctx: _Ctx) -> None:
    """INV-2/INV-3: bob can neither retrieve nor be cited alice's passages.

    Alice ingests a document; bob (same tenant, different owner) asks the same
    question. Bob's permission-filtered retrieval returns nothing, so the runtime
    must produce a zero-citation answer — alice's passage never leaks into bob's
    answer or citations.
    """
    await _upload_and_ingest(ctx, owner=ctx.alice, filename="tax-guide.txt", text=_TAX_TEXT)

    session_id, _ = await _ask(
        ctx, asker=ctx.bob, question="What is the 2024 standard deduction for a single filer?"
    )
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(session_id)
        assistant = [m for m in messages if m.role.value == "assistant"][0]
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message(assistant.id)
    # Bob's answer carries NO citation (alice's passage is outside his allow-set).
    assert citations == []


async def test_unanswerable_question_is_honest_refusal(ctx: _Ctx) -> None:
    """A question with no supporting source yields a grounded, zero-citation answer."""
    await _upload_and_ingest(ctx, owner=ctx.alice, filename="tax-guide.txt", text=_TAX_TEXT)

    session_id, _ = await _ask(
        ctx, asker=ctx.alice, question="What is the company's stock ticker symbol?"
    )
    async with ctx.sessionmaker() as session:
        messages = await MessageRepository(session, ctx.tenant_id).list_for_session(session_id)
        assistant = [m for m in messages if m.role.value == "assistant"][0]
        citations = await CitationRepository(session, ctx.tenant_id).list_for_message(assistant.id)
    assert citations == []
