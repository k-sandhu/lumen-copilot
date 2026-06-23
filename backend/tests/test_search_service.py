"""SearchService unit tests — the GET /search use-case (#83).

Two layers, both honouring the offline-safe pattern of the retrieval tests (#45):

* **Offline (in-memory SQLite)** — the service is driven with a **fake retrieval
  chokepoint** (so no pgvector is needed) seeded over real ``db/`` rows, exercising
  result projection (title / snippet / match spans / owner / freshness /
  permission), the opaque rank cursor, the source/type corpus narrowing, the
  optional cited direct answer (a fake gateway), ``hidden_count``, and the audit
  emit (INV-6). Defense-in-depth: the service re-asserts ownership during
  enrichment, so a passage whose document is not the caller's is dropped.
* **Live (compose Postgres + pgvector)** — the headline INV-1/INV-2 negative runs
  end-to-end through the **real** ``RetrievalService.search`` (the permission
  chokepoint) so the search results are proven to exclude another user's / another
  tenant's matching passage. Skips automatically when Postgres is unreachable.
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
from app.domain.entities import DocumentStatus, Role
from app.domain.llm import ChatMessage, Completion, Embedding, TokenUsage
from app.domain.retrieval import RetrievedPassage
from app.services.audit import AuditSink
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


async def test_drops_passage_for_foreign_owner_document(session: AsyncSession) -> None:
    """Defense in depth: a passage whose document is not the caller's is not surfaced.

    Even if a (hypothetically buggy) retrieval returned a foreign-owner passage,
    the enrichment step re-asserts ownership and drops it — INV-2 fail-closed.
    """
    tenant = (await TenantRepository(session).create(name="T")).id
    user_a, _doc_a, _ = await _seed_document(
        session, tenant_id=tenant, email="a@x.test", filename="a.txt", chunk_texts=["a"]
    )
    user_b, doc_b, chunk_b = await _seed_document(
        session, tenant_id=tenant, email="b@x.test", filename="b.txt", chunk_texts=["secret"]
    )
    # The fake retrieval is told to (wrongly) return user B's passage to user A.
    retrieval = _FakeRetrieval(
        [_passage(chunk_id=chunk_b[0], document_id=doc_b, name="b.txt", text="secret")]
    )
    svc = _service(
        session,
        principal=_principal(user_a, tenant),
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


_live = pytest.mark.skipif(
    not _pg_reachable(_PG_URL),
    reason=(
        f"Postgres not reachable for {_PG_URL}; live search permission test skipped "
        "(offline-safe). The real pgvector + full-text chokepoint runs only here."
    ),
)


def _unit_vector(dim: int, hot: int) -> list[float]:
    v = [0.0] * dim
    v[hot % dim] = 1.0
    return v


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

    engine = _create(_PG_URL)
    schema = f"search_test_{uuid.uuid4().hex[:8]}"
    hot = 11
    query_text = "annual revenue growth strategy"
    matching = "annual revenue growth strategy for the year"
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            await sess.execute(sql_text(f'SET search_path TO "{schema}", public'))
            tenant_a = (await TenantRepository(sess).create(name="A")).id
            tenant_b = (await TenantRepository(sess).create(name="B")).id

            async def _seed(
                tenant: uuid.UUID, email: str, fname: str
            ) -> tuple[uuid.UUID, uuid.UUID]:
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
                return user.id, doc.id

            user_a, doc_a = await _seed(tenant_a, "a@x.test", "a.txt")
            _user_b, doc_b = await _seed(tenant_b, "b@y.test", "b.txt")  # other tenant
            _user_c, doc_c = await _seed(tenant_a, "c@x.test", "c.txt")  # other owner, same tenant
            await sess.commit()

            gateway = _LiveGateway(_unit_vector(_EMBED_DIM, hot))
            retrieval = RetrievalService(sess, gateway=gateway)  # type: ignore[arg-type]
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
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
