"""Search API tests — the GET /search contract, auth gate, and audit (#83).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB (the
collections/documents API pattern). The #45 retrieval chokepoint's hybrid query
needs pgvector, which SQLite lacks, so the router's retrieval dependency is
overridden with a **fake** that returns a scripted, per-principal passage list —
the real chokepoint's permission filter (owner OR grant, INV-2) is proven in
``test_search_service``'s live tests. The fakes still flow through the service's
tenant-scoped enrichment + the real ``db/`` rows, so cross-tenant isolation
(INV-1) is exercised here too.

Covers:

* the 200 ``SearchResponse`` contract shape (query / results / hidden_count /
  next_cursor; ``SearchResult`` fields incl. match_spans, why_matched, permission,
  last_indexed) — assertions check the **contract**, not a hand-rolled duplicate;
* the optional cited ``direct_answer`` (scripted gateway) — citations reference
  result ids present in the page (INV-3);
* negatives: missing token → 401 problem+json (INV-4); a cross-tenant passage is
  trimmed at enrichment (INV-1); malformed cursor → 422 (INV-8);
* the search emits a ``retrieval.query`` audit event (INV-6).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db_session
from app.api.v1.search import get_llm_gateway_dep, get_retrieval_service
from app.auth import hash_password
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
from app.main import create_app

import app.db.models  # noqa: F401  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice_id: uuid.UUID,
        bob_id: uuid.UUID,
        alice_doc: uuid.UUID,
        alice_chunk: uuid.UUID,
        carol_doc: uuid.UUID,
        carol_chunk: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.alice_email = "alice@acme.test"
        self.carol_email = "carol@globex.test"
        self.alice_doc = alice_doc
        self.alice_chunk = alice_chunk
        # Carol is in tenant B — her document is the cross-tenant (INV-1) fixture
        # the enrichment step trims even if a buggy retrieval hands it back.
        self.carol_doc = carol_doc
        self.carol_chunk = carol_chunk


# --- Fakes injected into the router ----------------------------------------


class _FakeRetrieval:
    """Returns a fixed passage for every search (per the test seed).

    Enrichment re-reads each document through the *tenant-scoped* repository, so a
    passage whose document is in another tenant is dropped — this is how the
    cross-tenant (INV-1) negative is exercised offline. The same-tenant
    owner-OR-grant decision (INV-2) belongs to the retrieval chokepoint, which the
    fake stands in for here.
    """

    def __init__(self, passage: RetrievedPassage) -> None:
        self._passage = passage

    async def search(
        self, *, principal: Principal, query: str, k: int, collection_ids: object = None
    ) -> list[RetrievedPassage]:
        return [self._passage]


class _DisabledGateway:
    enabled = False

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> Completion:  # pragma: no cover
        raise AssertionError("disabled")

    async def embed(self, inputs: list[str]) -> list[Embedding]:  # pragma: no cover
        return [Embedding(vector=[0.0], model="fake") for _ in inputs]


class _AnsweringGateway:
    enabled = True

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        return Completion(
            content="The 2024 standard deduction is $14,600.", model="fake", usage=TokenUsage()
        )

    async def embed(self, inputs: list[str]) -> list[Embedding]:  # pragma: no cover
        return [Embedding(vector=[0.0], model="fake") for _ in inputs]


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as seed:
            ta = await TenantRepository(seed).create(name="Acme")
            tb = await TenantRepository(seed).create(name="Globex")
            alice = await UserRepository(seed, ta.id).create(
                email="alice@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash=hash_password(_PASSWORD), roles=[Role.MEMBER]
            )
            carol = await UserRepository(seed, tb.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            coll_a = await CollectionRepository(seed, ta.id).create(owner_id=alice.id, name="c")
            doc_a = await DocumentRepository(seed, ta.id).create(
                owner_id=alice.id,
                collection_id=coll_a.id,
                filename="taxes.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=f"{ta.id}/taxes.pdf",
                status=DocumentStatus.READY,
            )
            chunks_a = await ChunkRepository(seed, ta.id).replace_for_document(
                doc_a.id,
                [ChunkInput(text="2024 standard deduction is $14,600.", char_start=0, char_end=34)],
            )
            # Carol's document lives in tenant B — the cross-tenant fixture for the
            # enrichment trim test (INV-1).
            coll_c = await CollectionRepository(seed, tb.id).create(owner_id=carol.id, name="cc")
            doc_c = await DocumentRepository(seed, tb.id).create(
                owner_id=carol.id,
                collection_id=coll_c.id,
                filename="carol-secret.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                storage_key=f"{tb.id}/carol-secret.pdf",
                status=DocumentStatus.READY,
            )
            chunks_c = await ChunkRepository(seed, tb.id).replace_for_document(
                doc_c.id,
                [ChunkInput(text="carol's confidential figures", char_start=0, char_end=28)],
            )
            await seed.commit()
            factory.lumen_seeded = _Seeded(  # type: ignore[attr-defined]
                tenant_a=ta.id,
                tenant_b=tb.id,
                alice_id=alice.id,
                bob_id=bob.id,
                alice_doc=doc_a.id,
                alice_chunk=chunks_a[0].id,
                carol_doc=doc_c.id,
                carol_chunk=chunks_c[0].id,
            )
            yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


def _alice_passage(seeded: _Seeded) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=seeded.alice_chunk,
        document_id=seeded.alice_doc,
        document_name="taxes.pdf",
        ord=0,
        text="2024 standard deduction is $14,600.",
        char_start=0,
        char_end=34,
        score=0.9,
    )


def _carol_passage(seeded: _Seeded) -> RetrievedPassage:
    return RetrievedPassage(
        chunk_id=seeded.carol_chunk,
        document_id=seeded.carol_doc,
        document_name="carol-secret.pdf",
        ord=0,
        text="carol's confidential figures",
        char_start=0,
        char_end=28,
        score=0.9,
    )


def _make_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    passage: RetrievedPassage,
    gateway: object,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
    application.dependency_overrides[get_retrieval_service] = lambda: _FakeRetrieval(passage)
    application.dependency_overrides[get_llm_gateway_dep] = lambda: gateway

    # The app lifespan best-effort touches MinIO; stub it so startup never reaches
    # the network (mirrors the chat/documents API tests).
    import app.main as main_module

    class _NoopStore:
        async def ensure_bucket(self) -> None:
            return None

    monkeypatch.setattr(main_module, "get_object_store", lambda: _NoopStore())
    return application


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    application = _make_app(
        sessionmaker,
        passage=_alice_passage(seeded),
        gateway=_DisabledGateway(),
        monkeypatch=monkeypatch,
    )
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client: AsyncClient, email: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Happy path: contract shape --------------------------------------------


async def test_search_returns_contract_shape(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/search", headers=_auth(token), params={"q": "deduction"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # SearchResponse: required keys present; additionalProperties forbidden.
    assert set(body) <= {"query", "results", "hidden_count", "direct_answer", "next_cursor"}
    assert {"query", "results", "hidden_count"} <= set(body)
    assert body["query"] == "deduction"
    assert body["hidden_count"] == 0
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["id"] == str(seeded.alice_chunk)
    assert result["title"] == "taxes.pdf"
    assert result["snippet"] == "2024 standard deduction is $14,600."
    assert result["source"] == "upload"
    assert result["type"] == "document"
    assert result["permission"] == "allowed"
    assert result["owner"] == str(seeded.alice_id)
    assert result["document_id"] == str(seeded.alice_doc)
    assert "last_indexed" in result
    # The query term "deduction" is highlighted with a span.
    spans = result["match_spans"]
    assert spans and result["snippet"][spans[0]["start"] : spans[0]["end"]].lower() == "deduction"


async def test_search_paginates_with_limit_and_cursor(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    # Only one permitted result exists, so a single page with no next_cursor.
    resp = await client.get(
        "/api/v1/search", headers=_auth(token), params={"q": "deduction", "limit": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    assert body.get("next_cursor") in (None,)  # exhausted


# --- Direct answer (optional, cited) ---------------------------------------


async def test_search_includes_cited_direct_answer_when_gateway_enabled(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _make_app(
        sessionmaker,
        passage=_alice_passage(seeded),
        gateway=_AnsweringGateway(),
        monkeypatch=monkeypatch,
    )
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await _login(ac, seeded.alice_email)
        resp = await ac.get("/api/v1/search", headers=_auth(token), params={"q": "deduction"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    answer = body["direct_answer"]
    assert answer is not None
    assert "14,600" in answer["text"]
    result_ids = {r["id"] for r in body["results"]}
    assert answer["citations"]  # INV-3: at least one citation
    for citation in answer["citations"]:
        assert citation["result_id"] in result_ids
    application.dependency_overrides.clear()


# --- Negatives --------------------------------------------------------------


async def test_search_without_token_is_401(client: AsyncClient) -> None:
    """INV-4: a missing bearer token is a 401 problem+json before any retrieval."""
    resp = await client.get("/api/v1/search", params={"q": "deduction"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


async def test_search_trims_cross_tenant_results(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-1: a passage from another tenant is trimmed from Alice's results.

    The fake retrieval is wired to (wrongly) hand back Carol's tenant-B passage;
    the tenant-scoped :class:`DocumentRepository` cannot re-read it during
    enrichment, so it is dropped — Alice never sees a cross-tenant document, and
    the result set is empty. (Same-tenant ownership/grant is the chokepoint's job,
    proven end-to-end in ``test_search_service``'s live tests; enrichment's only
    re-check is tenant scope.)
    """
    application = _make_app(
        sessionmaker,
        passage=_carol_passage(seeded),  # Carol's tenant-B passage handed to Alice
        gateway=_DisabledGateway(),
        monkeypatch=monkeypatch,
    )
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        token = await _login(ac, seeded.alice_email)
        resp = await ac.get("/api/v1/search", headers=_auth(token), params={"q": "confidential"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"] == []
    assert str(seeded.carol_doc) not in resp.text
    application.dependency_overrides.clear()


async def test_search_rejects_malformed_cursor(client: AsyncClient, seeded: _Seeded) -> None:
    """INV-8: a forged cursor fails closed → 422, not silently page 1."""
    token = await _login(client, seeded.alice_email)
    resp = await client.get(
        "/api/v1/search", headers=_auth(token), params={"q": "deduction", "cursor": "garbage!!"}
    )
    assert resp.status_code == 422


async def test_search_missing_q_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    """The contract requires q (minLength 1); omitting it is a 422."""
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/search", headers=_auth(token))
    assert resp.status_code == 422


# --- Audit (INV-6) ----------------------------------------------------------


async def test_search_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/search", headers=_auth(token), params={"q": "deduction"})
    assert resp.status_code == 200

    async with sessionmaker() as sess:
        events = await AuditEventRepository(sess, seeded.tenant_a).list_recent()
    search_events = [
        e for e in events if e.action == "retrieval.query" and e.resource_type == "search"
    ]
    assert len(search_events) == 1
    assert search_events[0].actor_id == seeded.alice_id
