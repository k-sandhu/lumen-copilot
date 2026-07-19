"""Search suggest + recent-history API tests (spec 0005, epic #144).

End-to-end against the real FastAPI app over an offline in-memory SQLite DB. The
``document`` suggestions run through the REAL retrieval chokepoint
(``search_documents`` is SQL-only — no embeddings/gateway), so the load-bearing
negative (no cross-user document suggestions, INV-2) is exercised for real.

Covers:
* AC-G2 (critical): a prefix matching another user's document yields no document
  suggestion for it;
* AC-G1: completions from the caller's recent + saved searches;
* AC-R1/AC-R2: recent list newest-first, clear; /search records the query;
* AC-G3: all-whitespace q → 422; per-user isolation; no token → 401.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.api.v1.search as search_module
from app.api.deps import get_db_session
from app.auth import hash_password
from app.db.base import Base
from app.db.repositories import (
    CollectionRepository,
    DocumentRepository,
    RecentSearchRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role
from app.domain.retrieval import RetrievedPassage
from app.main import create_app

import app.db.models as models  # noqa: E402  isort: skip

_PASSWORD = "devpassword"


class _Seeded:
    def __init__(self, *, alice_id: uuid.UUID, bob_id: uuid.UUID) -> None:
        self.alice_id = alice_id
        self.bob_id = bob_id
        self.alice_email = "alice@acme.test"
        self.bob_email = "bob@acme.test"
        self.carol_email = "carol@globex.test"


async def _seed_doc(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, owner_id: uuid.UUID, name: str
) -> None:
    async with factory() as s:
        coll = await CollectionRepository(s, tenant_id).create(owner_id=owner_id, name="c")
        await DocumentRepository(s, tenant_id).create(
            owner_id=owner_id,
            collection_id=coll.id,
            filename=name,
            mime_type="application/pdf",
            size_bytes=10,
            storage_key=f"{tenant_id}/{name}",
            acl_enforced=False,
        )
        await s.commit()


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
            await UserRepository(seed, tb.id).create(
                email="carol@globex.test",
                password_hash=hash_password(_PASSWORD),
                roles=[Role.MEMBER],
            )
            await seed.commit()
            factory.lumen_tenant_a = ta.id  # type: ignore[attr-defined]
            factory.lumen_seeded = _Seeded(alice_id=alice.id, bob_id=bob.id)  # type: ignore[attr-defined]
        # Alice and Bob each own an "Acme …" document (same tenant, different owner).
        await _seed_doc(factory, ta.id, alice.id, "Acme Renewal Memo.pdf")
        await _seed_doc(factory, ta.id, bob.id, "Acme Secret Budget.pdf")
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.lumen_seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> Iterator[FastAPI]:
    application = create_app()

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_db_session] = _override_session
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


# --- AC-G2 (critical): document suggestions are permission-trimmed -----------


async def test_document_suggestions_are_permission_trimmed(
    client: AsyncClient, seeded: _Seeded
) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/search/suggest?q=acme&limit=10", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    docs = [s for s in resp.json()["suggestions"] if s["kind"] == "document"]
    names = {s["text"] for s in docs}
    # Alice sees her own "Acme …" document …
    assert "Acme Renewal Memo.pdf" in names
    # … and NEVER Bob's, even though it also matches the prefix (INV-2).
    assert "Acme Secret Budget.pdf" not in names


# --- AC-G1: completions from recent + saved ---------------------------------


async def test_completions_from_recent_and_saved(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    # Seed a recent query directly, and a saved search via the API.
    tenant_a = sessionmaker.lumen_tenant_a  # type: ignore[attr-defined]
    async with sessionmaker() as s:
        await RecentSearchRepository(s, tenant_a).record(seeded.alice_id, "acme renewal timeline")
        await s.commit()
    await client.post(
        "/api/v1/saved-searches", headers=_auth(token), json={"name": "Acme deals", "query": "acme"}
    )

    resp = await client.get("/api/v1/search/suggest?q=acme&limit=10", headers=_auth(token))
    assert resp.status_code == 200
    kinds = {s["kind"] for s in resp.json()["suggestions"]}
    assert "completion" in kinds  # from the recent query
    assert "saved_search" in kinds  # from the saved search name


# --- AC-R1 / AC-R2: recent list + clear, and /search records ----------------


async def test_recent_list_and_clear(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    token = await _login(client, seeded.alice_email)
    tenant_a = sessionmaker.lumen_tenant_a  # type: ignore[attr-defined]
    async with sessionmaker() as s:
        repo = RecentSearchRepository(s, tenant_a)
        await repo.record(seeded.alice_id, "first query")
        await repo.record(seeded.alice_id, "second query")
        await s.commit()

    listing = await client.get("/api/v1/search/recent", headers=_auth(token))
    assert listing.status_code == 200
    queries = [i["query"] for i in listing.json()["items"]]
    assert set(queries) == {"first query", "second query"}

    cleared = await client.delete("/api/v1/search/recent", headers=_auth(token))
    assert cleared.status_code == 204
    after = await client.get("/api/v1/search/recent", headers=_auth(token))
    assert after.json()["items"] == []


async def test_search_records_recent(client: AsyncClient, seeded: _Seeded, app: FastAPI) -> None:
    # Avoid the embedding path: a fake retrieval whose search returns nothing. The
    # recent-recording happens regardless (it precedes retrieval in the service).
    class _FakeRetrieval:
        async def search(
            self, *, principal: object, query: str, k: int, collection_ids: object = None
        ) -> list[RetrievedPassage]:
            return []

    app.dependency_overrides[search_module.get_retrieval_service] = lambda: _FakeRetrieval()
    token = await _login(client, seeded.alice_email)
    ran = await client.get("/api/v1/search?q=quarterly%20revenue", headers=_auth(token))
    assert ran.status_code == 200
    recent = await client.get("/api/v1/search/recent", headers=_auth(token))
    assert "quarterly revenue" in [i["query"] for i in recent.json()["items"]]


# --- AC-G3 + isolation + auth ----------------------------------------------


async def test_blank_query_is_422(client: AsyncClient, seeded: _Seeded) -> None:
    token = await _login(client, seeded.alice_email)
    resp = await client.get("/api/v1/search/suggest?q=%20%20%20", headers=_auth(token))
    assert resp.status_code == 422


async def test_recording_same_query_dedups(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Recording the same normalized query twice upserts ONE row (the atomic
    # ON CONFLICT DO UPDATE path) rather than racing/duplicating.
    tenant_a = sessionmaker.lumen_tenant_a  # type: ignore[attr-defined]
    async with sessionmaker() as s:
        repo = RecentSearchRepository(s, tenant_a)
        await repo.record(seeded.alice_id, "Acme Renewal")
        await repo.record(seeded.alice_id, "  acme   renewal ")  # same normalized form
        await s.commit()
    token = await _login(client, seeded.alice_email)
    items = (await client.get("/api/v1/search/recent", headers=_auth(token))).json()["items"]
    assert len(items) == 1  # deduped to a single row
    assert " ".join(items[0]["query"].lower().split()) == "acme renewal"


async def test_suggest_emits_audit_event(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # A suggest is a retrieval surface (it hits the chokepoint for document
    # matches), so it must leave one provable audit row (INV-6).
    token = await _login(client, seeded.alice_email)
    assert (
        await client.get("/api/v1/search/suggest?q=acme", headers=_auth(token))
    ).status_code == 200
    tenant_a = sessionmaker.lumen_tenant_a  # type: ignore[attr-defined]
    async with sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    select(models.AuditEvent).where(
                        models.AuditEvent.tenant_id == tenant_a,
                        models.AuditEvent.resource_type == "suggest",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].action == "retrieval.query"


async def test_recents_are_per_user(
    client: AsyncClient, seeded: _Seeded, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    tenant_a = sessionmaker.lumen_tenant_a  # type: ignore[attr-defined]
    async with sessionmaker() as s:
        await RecentSearchRepository(s, tenant_a).record(seeded.alice_id, "alice only")
        await s.commit()
    bob = await _login(client, seeded.bob_email)
    resp = await client.get("/api/v1/search/recent", headers=_auth(bob))
    assert resp.json()["items"] == []


async def test_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/search/suggest?q=x")).status_code == 401
    assert (await client.get("/api/v1/search/recent")).status_code == 401
    assert (await client.delete("/api/v1/search/recent")).status_code == 401
