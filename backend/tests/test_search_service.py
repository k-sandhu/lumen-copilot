"""SearchService unit tests — the GET /search use-case (#83).

Two layers, both honouring the offline-safe pattern of the retrieval tests (#45):

* **Offline (in-memory SQLite)** — the service is driven with a **fake retrieval
  chokepoint** (so no pgvector is needed) seeded over real ``db/`` rows, exercising
  result projection (title / snippet / match spans / owner / freshness /
  permission), the opaque rank cursor, the source/type corpus narrowing, the
  optional cited direct answer (a fake gateway), ``hidden_count``, and the audit
  emit (INV-6). Enrichment trusts the chokepoint for INV-2 (owner **or** grant)
  and only re-checks the tenant scope (INV-1): a passage whose document is in
  another tenant is dropped, but a same-tenant non-owned passage the chokepoint
  surfaced (e.g. a grant) is projected — it is not re-narrowed to ownership.
* **Live (Postgres + OpenSearch)** — the headline INV-1/INV-2 cases run
  end-to-end through the **real** ``RetrievalService.search`` (the engine-backed
  permission chokepoint, ADR-0010): the negatives prove results exclude another
  user's / another tenant's matching passage, and the grant positive proves a
  document granted to the caller *does* come back from ``/search`` (matching what
  suggest surfaces). Skips automatically when Postgres or OpenSearch is unreachable.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator, Sequence
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, GrantResourceType, Role
from app.domain.llm import ChatMessage, Completion, Embedding, TokenUsage
from app.domain.retrieval import RetrievedPassage
from app.search import OpenSearchStore
from app.services.audit import AuditSink
from app.services.grants_service import GrantsService
from app.services.search_service import SearchService

import app.db.models  # noqa: F401  isort: skip

_EMBED_DIM = 1024


# --- Fakes ------------------------------------------------------------------


class _FakeRetrieval:
    """A stand-in for the #45 chokepoint that returns a scripted passage list.

    Records the principal + query + k it was called with so a test can assert the
    service forwarded the *resolved* principal (the allow-set source, INV-2) and
    over-fetched for pagination, without needing pgvector.
    """

    def __init__(self, passages: list[RetrievedPassage]) -> None:
        self._passages = passages
        self.calls: list[dict[str, object]] = []

    async def search(
        self,
        *,
        principal: Principal,
        query: str,
        k: int,
        collection_ids: list[uuid.UUID] | None = None,
    ) -> list[RetrievedPassage]:
        self.calls.append(
            {"principal": principal, "query": query, "k": k, "collection_ids": collection_ids}
        )
        return list(self._passages)


class _DisabledGateway:
    """A gateway with no provider configured — the direct answer is skipped."""

    enabled = False

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str | None = None
    ) -> Completion:  # pragma: no cover
        raise AssertionError("chat must not be called when the gateway is disabled")

    async def embed(self, inputs: list[str]) -> list[Embedding]:  # pragma: no cover
        return [Embedding(vector=[0.0] * _EMBED_DIM, model="fake") for _ in inputs]


class _AnsweringGateway:
    """A gateway that returns a fixed grounded answer (the direct-answer path)."""

    enabled = True

    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.calls: list[list[ChatMessage]] = []

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str | None = None
    ) -> Completion:
        self.calls.append(list(messages))
        return Completion(content=self._answer, model="fake", usage=TokenUsage())

    async def embed(self, inputs: list[str]) -> list[Embedding]:  # pragma: no cover
        return [Embedding(vector=[0.0] * _EMBED_DIM, model="fake") for _ in inputs]


def _principal(user_id: uuid.UUID, tenant_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(Role.MEMBER,))


# --- Offline fixture: SQLite schema + a seeded user/document ----------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


async def _seed_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    email: str,
    filename: str,
    chunk_texts: list[str],
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """Create a user + collection + document with chunks; return (user, doc, chunk ids)."""
    user = await UserRepository(session, tenant_id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_id).create(owner_id=user.id, name="C")
    doc = await DocumentRepository(session, tenant_id).create(
        owner_id=user.id,
        collection_id=coll.id,
        filename=filename,
        mime_type="text/plain",
        size_bytes=10,
        storage_key="k",
        status=DocumentStatus.READY,
    )
    offset = 0
    inputs: list[ChunkInput] = []
    for text in chunk_texts:
        inputs.append(ChunkInput(text=text, char_start=offset, char_end=offset + len(text)))
        offset += len(text)
    chunks = await ChunkRepository(session, tenant_id).replace_for_document(doc.id, inputs)
    await session.commit()
    return user.id, doc.id, [c.id for c in chunks]


def _passage(
    *, chunk_id: uuid.UUID, document_id: uuid.UUID, name: str, text: str
) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=chunk_id,
        document_id=document_id,
        document_name=name,
        ord=0,
        text=text,
        char_start=0,
        char_end=len(text),
        score=0.9,
    )


def _service(
    session: AsyncSession,
    *,
    principal: Principal,
    retrieval: _FakeRetrieval,
    gateway: object,
) -> SearchService:
    audit = AuditSink(AuditEventRepository(session, principal.tenant_id))
    return SearchService(
        session,
        principal=principal,
        gateway=gateway,  # type: ignore[arg-type]
        audit=audit,
        retrieval=retrieval,  # type: ignore[arg-type]
        request_id="req-1",
        source_ip="127.0.0.1",
    )


# --- Result projection ------------------------------------------------------


async def test_projects_passage_into_contract_result(session: AsyncSession) -> None:
    """A permitted passage becomes a SearchResult with owner + freshness + spans."""
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session,
        tenant_id=tenant,
        email="a@x.test",
        filename="q3-budget.txt",
        chunk_texts=["the quarterly budget figures"],
    )
    retrieval = _FakeRetrieval(
        [
            _passage(
                chunk_id=chunk_ids[0],
                document_id=doc,
                name="q3-budget.txt",
                text="the quarterly budget figures",
            )
        ]
    )
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    page = await svc.search(query="budget")

    assert page.query == "budget"
    assert len(page.results) == 1
    result = page.results[0]
    assert result.id == chunk_ids[0]
    assert result.title == "q3-budget.txt"
    assert result.snippet == "the quarterly budget figures"
    assert result.source == "upload"
    assert result.type == "document"
    assert result.permission == "allowed"
    assert result.owner == user
    assert result.document_id == doc
    assert result.score == pytest.approx(0.9)
    # The lexical term "budget" is highlighted with a span.
    assert any(result.snippet[s.start : s.end].lower() == "budget" for s in result.match_spans)
    assert "lexical" in result.why_matched.lower()


async def test_forwards_resolved_principal_to_chokepoint(session: AsyncSession) -> None:
    """INV-2: the service keys retrieval off the *resolved* principal, never request input."""
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["alpha"]
    )
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="alpha")]
    )
    principal = _principal(user, tenant)
    svc = _service(session, principal=principal, retrieval=retrieval, gateway=_DisabledGateway())

    await svc.search(query="alpha", collection_id=None)

    assert retrieval.calls[0]["principal"] is principal


async def test_projects_same_tenant_non_owned_passage_from_chokepoint(
    session: AsyncSession,
) -> None:
    """Enrichment trusts the chokepoint for INV-2 — it does NOT re-narrow to ownership.

    A same-tenant passage the chokepoint surfaced for a document the caller does
    not own (e.g. one explicitly granted to them) must be projected, not dropped:
    re-asserting strict ownership here would wrongly hide grant-visible documents
    (the regression this fix targets — the chokepoint, not enrichment, is the INV-2
    authority). The projected result carries the document's real owner. (The grant
    *enforcement* is proven end-to-end by the live test; offline the fake retrieval
    stands in for the chokepoint's owner-or-grant decision.)
    """
    tenant = (await TenantRepository(session).create(name="T")).id
    user_a, _doc_a, _ = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="a.txt", chunk_texts=["a"]
    )
    user_b, doc_b, chunk_b = await _seed_document(
        session, tenant_id=tenant, email="b@x.test", filename="b.txt", chunk_texts=["shared"]
    )
    # The chokepoint (here the fake) permitted user B's passage for user A — the
    # owner-or-grant decision it owns. Enrichment must surface it, not re-narrow.
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_b[0], document_id=doc_b, name="b.txt", text="shared")]
    )
    svc = _service(
        session,
        principal=_principal(user_a, tenant),
        retrieval=retrieval,
        gateway=_DisabledGateway(),
    )

    page = await svc.search(query="shared")

    assert [r.document_id for r in page.results] == [doc_b]
    assert page.results[0].owner == user_b  # the document's real owner, not the caller
    assert page.results[0].permission == "allowed"


async def test_drops_passage_for_foreign_tenant_document(session: AsyncSession) -> None:
    """INV-1 defense-in-depth kept: a cross-tenant document is dropped at enrichment.

    The tenant-scoped :class:`DocumentRepository` cannot re-read a document outside
    the caller's tenant, so even if a (hypothetically buggy) retrieval returned a
    foreign-tenant passage, enrichment yields no metadata and drops it — tenant
    scope is the one re-check enrichment still performs.
    """
    tenant_a = (await TenantRepository(session).create(name="A")).id
    tenant_b = (await TenantRepository(session).create(name="B")).id
    user_a, _doc_a, _ = await _seed_document(
        session, tenant_id=tenant_a, email="a@x.test", filename="a.txt", chunk_texts=["a"]
    )
    _user_b, doc_b, chunk_b = await _seed_document(
        session, tenant_id=tenant_b, email="b@y.test", filename="b.txt", chunk_texts=["secret"]
    )
    # The fake retrieval (wrongly) returns a tenant-B passage to a tenant-A caller.
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_b[0], document_id=doc_b, name="b.txt", text="secret")]
    )
    svc = _service(
        session,
        principal=_principal(user_a, tenant_a),
        retrieval=retrieval,
        gateway=_DisabledGateway(),
    )

    page = await svc.search(query="secret")

    assert page.results == []


# --- Corpus narrowing (source / type) ---------------------------------------


async def test_non_upload_source_yields_empty_page(session: AsyncSession) -> None:
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="x")]
    )
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    page = await svc.search(query="x", source="connector")

    assert page.results == []
    # The chokepoint was not even queried for an unsatisfiable corpus.
    assert retrieval.calls == []


async def test_non_document_type_yields_empty_page(session: AsyncSession) -> None:
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="x")]
    )
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    page = await svc.search(query="x", content_type="message")

    assert page.results == []


# --- Pagination -------------------------------------------------------------


async def test_pagination_cursor_round_trip(session: AsyncSession) -> None:
    """A page emits a next_cursor; the next page resumes after it and ends clean."""
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, _ = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    # 3 distinct passages (ids unique); page size 2 → page 1 (2) + cursor, page 2 (1) none.
    passages = [
        _passage(chunk_id=uuid.uuid4(), document_id=doc, name="f.txt", text=f"chunk {i}")
        for i in range(3)
    ]
    retrieval = _FakeRetrieval(passages)
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    first = await svc.search(query="chunk", limit=2)
    assert len(first.results) == 2
    assert first.next_cursor is not None
    # Over-fetched window+1 to know a next page exists.
    assert retrieval.calls[0]["k"] == 0 + 2 + 1

    second = await svc.search(query="chunk", limit=2, cursor=first.next_cursor)
    assert len(second.results) == 1
    assert second.next_cursor is None
    # The two pages cover distinct results (no overlap).
    assert {r.id for r in first.results}.isdisjoint({r.id for r in second.results})


class _KRespectingRetrieval:
    """A retrieval fake that HONOURS ``k`` like the real chokepoint — returns the
    top ``k`` of a ranked corpus. Lets a test exercise the pagination boundary at
    the retrieval ceiling (the plain ``_FakeRetrieval`` ignores ``k``)."""

    def __init__(self, available: list[RetrievedPassage]) -> None:
        self._available = available
        self.calls: list[int] = []

    async def search(
        self,
        *,
        principal: Principal,
        query: str,
        k: int,
        collection_ids: list[uuid.UUID] | None = None,
    ) -> list[RetrievedPassage]:
        self.calls.append(k)
        return list(self._available[:k])


async def test_pagination_terminates_at_retrieval_ceiling_without_empty_page(
    session: AsyncSession,
) -> None:
    """With more matches than retrieval can rank, pagination walks to the MAX_K
    ceiling, never emits a silently-empty page, and never hands out a next_cursor
    past the reachable band (#270)."""
    from app.retrieval import MAX_K

    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, _ = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    # A corpus larger than the retrieval ceiling can rank.
    corpus = [
        _passage(chunk_id=uuid.uuid4(), document_id=doc, name="f.txt", text=f"c{i}")
        for i in range(MAX_K + 25)
    ]
    retrieval = _KRespectingRetrieval(corpus)
    svc = _service(
        session,
        principal=_principal(user, tenant),
        retrieval=retrieval,  # type: ignore[arg-type]
        gateway=_DisabledGateway(),
    )

    seen = 0
    cursor: str | None = None
    pages = 0
    while True:
        page = await svc.search(query="c", limit=20, cursor=cursor)
        pages += 1
        assert len(page.results) > 0, "a page must never be silently empty (#270)"
        seen += len(page.results)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert pages < 20, "pagination must terminate, not loop"

    # The reachable set is exactly the retrieval ceiling — nothing past rank MAX_K
    # is advertised, and the walk stopped cleanly (no dead cursor).
    assert seen == MAX_K
    # A single wide page terminates just as honestly: MAX_K results, no next page.
    big = await svc.search(query="c", limit=100)
    assert len(big.results) == MAX_K
    assert big.next_cursor is None
    # The service never asks retrieval for more than the ceiling.
    assert max(retrieval.calls) <= MAX_K


async def test_malformed_cursor_is_rejected(session: AsyncSession) -> None:
    """A forged/garbled cursor fails closed → ValidationError (422), not page 1 (INV-8)."""
    from app.core.errors import ValidationError

    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="x")]
    )
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    with pytest.raises(ValidationError):
        await svc.search(query="x", cursor="not-a-real-cursor!!")


# --- hidden_count -----------------------------------------------------------


async def test_hidden_count_is_zero_in_mvp(session: AsyncSession) -> None:
    """MVP: nothing is permitted-but-hidden (structural filter), so hidden_count == 0."""
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="x")]
    )
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    page = await svc.search(query="x")

    assert page.hidden_count == 0


# --- Optional cited direct answer (INV-3) -----------------------------------


async def test_no_direct_answer_when_gateway_disabled(session: AsyncSession) -> None:
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["budget is $5"]
    )
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="budget is $5")]
    )
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    page = await svc.search(query="budget")

    assert page.direct_answer is None
    assert len(page.results) == 1  # results still returned


async def test_direct_answer_cites_results_present_in_page(session: AsyncSession) -> None:
    """INV-3: every citation references a result id present in this page's results."""
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session,
        tenant_id=tenant,
        email="a@x.test",
        filename="f.txt",
        chunk_texts=["the 2024 budget is $5M"],
    )
    retrieval = _FakeRetrieval(
        [
            _passage(
                chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="the 2024 budget is $5M"
            )
        ]
    )
    gateway = _AnsweringGateway("The 2024 budget is $5M.")
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=gateway
    )

    page = await svc.search(query="budget")

    assert page.direct_answer is not None
    assert page.direct_answer.text == "The 2024 budget is $5M."
    result_ids = {r.id for r in page.results}
    assert page.direct_answer.citations
    for citation in page.direct_answer.citations:
        assert citation.result_id in result_ids


async def test_direct_answer_omitted_when_no_results(session: AsyncSession) -> None:
    """No permitted results → no answer attempt (prefer 'no answer', mission filter #2)."""
    tenant = (await TenantRepository(session).create(name="T")).id
    user, _doc, _ = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["x"]
    )
    retrieval = _FakeRetrieval([])  # retrieval found nothing permitted
    gateway = _AnsweringGateway("should not be called")
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=gateway
    )

    page = await svc.search(query="nothing matches")

    assert page.results == []
    assert page.direct_answer is None
    assert gateway.calls == []  # the model was never grounded on an empty set


# --- Audit (INV-6) ----------------------------------------------------------


async def test_search_emits_one_audit_event(session: AsyncSession) -> None:
    """Every search emits exactly one retrieval.query event (auditable, INV-6)."""
    tenant = (await TenantRepository(session).create(name="T")).id
    user, doc, chunk_ids = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="f.txt", chunk_texts=["budget"]
    )
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_ids[0], document_id=doc, name="f.txt", text="budget")]
    )
    svc = _service(
        session, principal=_principal(user, tenant), retrieval=retrieval, gateway=_DisabledGateway()
    )

    await svc.search(query="budget")
    await session.commit()

    events = await AuditEventRepository(session, tenant).list_recent()
    search_events = [e for e in events if e.action == "retrieval.query"]
    assert len(search_events) == 1
    ev = search_events[0]
    assert ev.actor_id == user
    assert ev.resource_type == "search"
    # The raw query is never stored — a hash is (spec 0004 §2.4).
    assert ev.metadata["hit_count"] == 1
    assert str(doc) in ev.metadata["document_ids"]  # type: ignore[operator]
    assert "budget" not in str(ev.metadata)


# --- Live INV-1/INV-2 through the real chokepoint (Postgres-gated) ----------

_PG_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumen"
)


def _pg_reachable(url: str) -> bool:
    parsed = urlparse(url.replace("postgresql+asyncpg", "postgresql"))
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_OS_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:47186")


def _os_reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 9200), timeout=1.0
        ):
            return True
    except OSError:
        return False


_live = pytest.mark.skipif(
    not (_pg_reachable(_PG_URL) and _os_reachable(_OS_URL)),
    reason=(
        f"Postgres ({_PG_URL}) and OpenSearch ({_OS_URL}) must both be reachable; "
        "live search permission test skipped (offline-safe). The engine-backed "
        "chokepoint (ADR-0010) runs only here."
    ),
)


def _unit_vector(dim: int, hot: int) -> list[float]:
    v = [0.0] * dim
    v[hot % dim] = 1.0
    return v


async def _index_document_chunks(
    store: OpenSearchStore,
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    owner_id: uuid.UUID,
    collection_id: uuid.UUID,
) -> None:
    """Index a seeded document's chunks into the per-test engine index (ADR-0010).

    The live tests seed chunks straight through the repository (no ingestion
    task), so they must mirror the write-path into OpenSearch themselves before
    the engine-backed search can find them.
    """
    from app.search import IndexedChunk

    chunks = await ChunkRepository(session, tenant_id).list_for_document(document_id)
    await store.upsert_chunks(
        [
            IndexedChunk(
                chunk_id=c.id,
                tenant_id=c.tenant_id,
                document_id=c.document_id,
                owner_id=owner_id,
                collection_id=collection_id,
                ord=c.ord,
                text=c.text,
                embedding=c.embedding,
                char_start=c.char_start,
                char_end=c.char_end,
            )
            for c in chunks
        ],
        refresh=True,
    )


async def _drop_index(store: OpenSearchStore) -> None:
    """Delete the per-test index + pipeline and close the store (teardown)."""
    import httpx as _httpx

    async with _httpx.AsyncClient(base_url=_OS_URL, timeout=15.0) as cleanup:
        await cleanup.delete(f"/{store._index}")  # noqa: SLF001 — test teardown
        await cleanup.delete(f"/_search/pipeline/{store._index}-hybrid")  # noqa: SLF001
    await store.aclose()


class _LiveGateway:
    """A no-network embed for the live search; the direct answer stays disabled."""

    enabled = False

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def chat(
        self, messages: Sequence[ChatMessage], *, model: str | None = None
    ) -> Completion:  # pragma: no cover
        raise AssertionError("disabled gateway")

    async def embed(self, inputs: list[str]) -> list[Embedding]:
        return [Embedding(vector=list(self._vector), model="fake") for _ in inputs]


@_live
async def test_live_search_excludes_other_tenant_and_owner() -> None:
    """End-to-end on real Postgres: GET /search results exclude foreign passages (INV-1/INV-2).

    Two users (one in another tenant, one another owner in the same tenant) each
    own a document whose chunk text + embedding match the query. User A's search,
    routed through the real ``RetrievalService.search`` chokepoint, returns only
    A's passage — never B's or C's, proving the permission filter holds at the
    search surface, not just inside retrieval.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine as _create

    from app.retrieval import RetrievalService
    from app.search import OpenSearchStore

    engine = _create(_PG_URL)
    schema = f"search_test_{uuid.uuid4().hex[:8]}"
    store = OpenSearchStore(
        base_url=_OS_URL,
        index=f"lumen-test-{uuid.uuid4().hex[:8]}",
        dimensions=_EMBED_DIM,
        timeout_seconds=30.0,
    )
    hot = 11
    query_text = "annual revenue growth strategy"
    matching = "annual revenue growth strategy for the year"
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)
        await store.ensure_index()

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            await sess.execute(sql_text(f'SET search_path TO "{schema}", public'))
            tenant_a = (await TenantRepository(sess).create(name="A")).id
            tenant_b = (await TenantRepository(sess).create(name="B")).id

            async def _seed(
                tenant: uuid.UUID, email: str, fname: str
            ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
                user = await UserRepository(sess, tenant).create(
                    email=email, password_hash="h", roles=[Role.MEMBER]
                )
                coll = await CollectionRepository(sess, tenant).create(owner_id=user.id, name="C")
                doc = await DocumentRepository(sess, tenant).create(
                    owner_id=user.id,
                    collection_id=coll.id,
                    filename=fname,
                    mime_type="text/plain",
                    size_bytes=1,
                    storage_key="k",
                    status=DocumentStatus.READY,
                )
                await ChunkRepository(sess, tenant).replace_for_document(
                    doc.id,
                    [
                        ChunkInput(
                            text=matching,
                            char_start=0,
                            char_end=len(matching),
                            embedding=_unit_vector(_EMBED_DIM, hot),
                        )
                    ],
                )
                return user.id, doc.id, coll.id

            user_a, doc_a, coll_a = await _seed(tenant_a, "a@x.test", "a.txt")
            user_b, doc_b, coll_b = await _seed(tenant_b, "b@y.test", "b.txt")  # other tenant
            user_c, doc_c, coll_c = await _seed(tenant_a, "c@x.test", "c.txt")  # other owner
            await sess.commit()

            # Index all three corpora into the per-test engine index (ADR-0010).
            for tid, did, oid, cid in (
                (tenant_a, doc_a, user_a, coll_a),
                (tenant_b, doc_b, user_b, coll_b),
                (tenant_a, doc_c, user_c, coll_c),
            ):
                await _index_document_chunks(
                    store, sess, tenant_id=tid, document_id=did, owner_id=oid, collection_id=cid
                )

            gateway = _LiveGateway(_unit_vector(_EMBED_DIM, hot))
            retrieval = RetrievalService(sess, gateway=gateway, store=store)  # type: ignore[arg-type]
            audit = AuditSink(AuditEventRepository(sess, tenant_a))
            svc = SearchService(
                sess,
                principal=_principal(user_a, tenant_a),
                gateway=gateway,  # type: ignore[arg-type]
                audit=audit,
                retrieval=retrieval,
                request_id="req-live",
                source_ip="127.0.0.1",
            )

            page = await svc.search(query=query_text, limit=10)

            doc_ids = {r.document_id for r in page.results}
            assert doc_a in doc_ids
            assert doc_b not in doc_ids  # INV-1: other tenant excluded
            assert doc_c not in doc_ids  # INV-2: other owner excluded
            assert all(r.permission == "allowed" for r in page.results)
            assert all(r.owner == user_a for r in page.results)
    finally:
        await _drop_index(store)
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@_live
async def test_live_search_returns_granted_document_passages() -> None:
    """End-to-end on real Postgres: GET /search returns a *granted* document's passages.

    Regression for #181: ``_to_results`` used to re-narrow to strict ownership, so a
    document granted to the caller (admitted by the chokepoint's owner-OR-grant
    predicate) was surfaced in ``/search/suggest`` yet returned ZERO results from
    ``/search``. User A owns a document whose chunk text + embedding match the
    query; user B is a non-owner in the same tenant. Before any grant B's search
    returns nothing; after an explicit document grant to B, ``/search`` returns A's
    passage to B — with ``owner == user_a`` (the document's real owner, not the
    caller) and ``permission == "allowed"`` — proving search now matches what the
    chokepoint permits and what suggest already surfaces. Runs through the real
    ``RetrievalService.search`` chokepoint (pgvector + full-text), so it exercises
    the actual grant ``EXISTS``, not the offline fake.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine as _create

    from app.retrieval import RetrievalService
    from app.search import OpenSearchStore

    engine = _create(_PG_URL)
    schema = f"search_grant_test_{uuid.uuid4().hex[:8]}"
    store = OpenSearchStore(
        base_url=_OS_URL,
        index=f"lumen-test-{uuid.uuid4().hex[:8]}",
        dimensions=_EMBED_DIM,
        timeout_seconds=30.0,
    )
    hot = 13
    query_text = "annual revenue growth strategy"
    matching = "annual revenue growth strategy for the year"
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)
        await store.ensure_index()

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            await sess.execute(sql_text(f'SET search_path TO "{schema}", public'))
            tenant = (await TenantRepository(sess).create(name="A")).id

            owner = await UserRepository(sess, tenant).create(
                email="owner@x.test", password_hash="h", roles=[Role.MEMBER]
            )
            grantee = await UserRepository(sess, tenant).create(
                email="grantee@x.test", password_hash="h", roles=[Role.MEMBER]
            )
            coll = await CollectionRepository(sess, tenant).create(owner_id=owner.id, name="C")
            doc = await DocumentRepository(sess, tenant).create(
                owner_id=owner.id,
                collection_id=coll.id,
                filename="a.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key="k",
                status=DocumentStatus.READY,
            )
            await ChunkRepository(sess, tenant).replace_for_document(
                doc.id,
                [
                    ChunkInput(
                        text=matching,
                        char_start=0,
                        char_end=len(matching),
                        embedding=_unit_vector(_EMBED_DIM, hot),
                    )
                ],
            )
            await sess.commit()
            await _index_document_chunks(
                store, sess, tenant_id=tenant, document_id=doc.id,
                owner_id=owner.id, collection_id=coll.id,
            )

            gateway = _LiveGateway(_unit_vector(_EMBED_DIM, hot))
            retrieval = RetrievalService(sess, gateway=gateway, store=store)  # type: ignore[arg-type]

            def _search_as(user_id: uuid.UUID) -> SearchService:
                audit = AuditSink(AuditEventRepository(sess, tenant))
                return SearchService(
                    sess,
                    principal=_principal(user_id, tenant),
                    gateway=gateway,  # type: ignore[arg-type]
                    audit=audit,
                    retrieval=retrieval,
                    request_id="req-live",
                    source_ip="127.0.0.1",
                )

            # Before the grant: the non-owner's search returns nothing (deny-by-default).
            before = await _search_as(grantee.id).search(query=query_text, limit=10)
            assert before.results == []

            # Owner grants the non-owner viewer access to the document.
            grants = GrantsService(
                sess,
                tenant_id=tenant,
                owner_id=owner.id,
                roles=(Role.MEMBER,),
                audit=AuditSink(AuditEventRepository(sess, tenant)),
                request_id="req-live",
                source_ip="127.0.0.1",
            )
            await grants.create_grant(
                resource_type=GrantResourceType.DOCUMENT,
                resource_id=doc.id,
                principal_id=grantee.id,
            )
            await sess.commit()

            # After the grant: /search returns the granted document's passage —
            # the regression. (Previously this stayed empty because enrichment
            # re-narrowed to the caller's ownership.)
            after = await _search_as(grantee.id).search(query=query_text, limit=10)
            assert [r.document_id for r in after.results] == [doc.id]
            result = after.results[0]
            assert result.owner == owner.id  # the document's real owner, not the grantee
            assert result.permission == "allowed"
            assert result.title == "a.txt"
    finally:
        await _drop_index(store)
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
