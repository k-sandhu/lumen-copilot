"""Mode-split permission predicate tests — both chokepoints, in lockstep (#453).

ADR-0019 §2 / spec 0004 §2.2 (exclusive enforcement modes), pinned at the two
widened chokepoints:

* **Postgres** (``retrieval/queries._document_permitted``) — driven through the
  real query builders against in-memory SQLite (the predicate is written
  portably, so the offline suite proves the same SQL shape Postgres runs);
* **engine** (``search/filters.SearchAllowFilter.to_engine_filter``) — the
  filter's mode-split shape, asserted structurally.

The INV-2 negatives the issue mandates: owner denied and explicit grantee
denied on ``acl_enforced`` documents with empty mirrors; stale-beyond-window
and stale-stamped (NULL) denied; unattested/foreign principals excluded;
non-enforced upload/web behaviour byte-identical to before. Hydration
re-checks come free because ``load_passages`` routes through the same
predicate — asserted here explicitly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.db.repositories import (
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import DocumentStatus, Role
from app.retrieval import queries
from app.retrieval.permissions import AllowSet
from app.search.filters import SearchAllowFilter, acl_freshness_floor

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


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
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


class _World:
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    member_id: uuid.UUID
    grantee_id: uuid.UUID
    collection_id: uuid.UUID
    docs: dict[str, uuid.UUID]
    chunks: dict[str, uuid.UUID]


def _allow(world: _World, user_id: uuid.UUID) -> AllowSet:
    return AllowSet(
        tenant_id=world.tenant_id,
        owner_ids=frozenset({user_id}),
        grant_principal_id=user_id,
        acl_principals=frozenset({f"user:{user_id}", "tenant"}),
    )


@pytest_asyncio.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession]) -> _World:
    """Seed the mode-split matrix: one document per predicate case."""
    w = _World()
    fresh = datetime.now(UTC)
    beyond_window = acl_freshness_floor() - timedelta(hours=1)
    async with sessionmaker() as session:
        tenant = await TenantRepository(session).create(name="Acme")
        w.tenant_id = tenant.id
        users = UserRepository(session, tenant.id)
        w.owner_id = (
            await users.create(email="owner@acme.test", password_hash="h", roles=[Role.ADMIN])
        ).id
        w.member_id = (
            await users.create(email="member@acme.test", password_hash="h", roles=[Role.MEMBER])
        ).id
        w.grantee_id = (
            await users.create(email="grantee@acme.test", password_hash="h", roles=[Role.MEMBER])
        ).id
        collection = await CollectionRepository(session, tenant.id).create(
            owner_id=w.owner_id, name="c"
        )
        w.collection_id = collection.id
        documents = DocumentRepository(session, tenant.id)
        chunks = ChunkRepository(session, tenant.id)

        async def make(
            key: str,
            *,
            acl_enforced: bool,
            acl_principals: list[str] | None = None,
            acl_synced_at: datetime | None = None,
        ) -> None:
            doc = await documents.create(
                owner_id=w.owner_id,
                collection_id=w.collection_id,
                filename=f"{key}.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key=f"{tenant.id}/{key}",
                acl_enforced=acl_enforced,
                status=DocumentStatus.READY,
                acl_principals=acl_principals,
                acl_synced_at=acl_synced_at,
            )
            w.docs[key] = doc.id
            [chunk] = await chunks.replace_for_document(
                doc.id, [ChunkInput(text=f"text of {key}", char_start=0, char_end=10)]
            )
            w.chunks[key] = chunk.id

        w.docs = {}
        w.chunks = {}
        # Non-enforced upload — owner-or-grant, unchanged behaviour.
        await make("upload", acl_enforced=False)
        # Enforced, EMPTY mirror — nobody, including the owner (fail closed).
        await make("empty", acl_enforced=True, acl_principals=[], acl_synced_at=fresh)
        # Enforced, member admitted, fresh.
        await make(
            "member_fresh",
            acl_enforced=True,
            acl_principals=[f"user:{w.member_id}"],
            acl_synced_at=fresh,
        )
        # Enforced, member listed but STALE (beyond the window).
        await make(
            "member_stale",
            acl_enforced=True,
            acl_principals=[f"user:{w.member_id}"],
            acl_synced_at=beyond_window,
        )
        # Enforced, member listed but stale-STAMPED (NULL — immediate deny).
        await make(
            "member_stamped",
            acl_enforced=True,
            acl_principals=[f"user:{w.member_id}"],
            acl_synced_at=None,
        )
        # Enforced, tenant-wide (anyone-share), fresh.
        await make("tenant_wide", acl_enforced=True, acl_principals=["tenant"], acl_synced_at=fresh)
        # A Lumen grant on an enforced-empty document — must NOT widen it.
        session.add(
            models.Grant(
                tenant_id=tenant.id,
                resource_type="document",
                resource_id=w.docs["empty"],
                principal_type="user",
                principal_id=w.grantee_id,
                role="viewer",
                granted_by=w.owner_id,
            )
        )
        # The same grantee also holds a grant on the non-enforced upload —
        # proving the grant leg still works where it applies.
        session.add(
            models.Grant(
                tenant_id=tenant.id,
                resource_type="document",
                resource_id=w.docs["upload"],
                principal_type="user",
                principal_id=w.grantee_id,
                role="viewer",
                granted_by=w.owner_id,
            )
        )
        await session.commit()
    return w


async def _visible_docs(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World, user_id: uuid.UUID
) -> set[str]:
    async with sessionmaker() as session:
        rows = await queries.list_documents(session, allow_set=_allow(world, user_id), k=50)
    names = {r.document_name for r in rows}
    return {n.removesuffix(".txt") for n in names}


# --- Postgres predicate (the SQL chokepoint) ---------------------------------


async def test_owner_sees_upload_but_not_enforced_docs(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> None:
    """INV-2: ownership NEVER widens an acl_enforced document — the owner of
    the rows sees only their upload and the mirrors that list/`tenant` them."""
    visible = await _visible_docs(sessionmaker, world, world.owner_id)
    assert "upload" in visible
    assert "empty" not in visible  # owner denied on an empty mirror
    assert "member_fresh" not in visible  # mirror lists someone else
    assert "member_stale" not in visible
    assert "member_stamped" not in visible
    assert "tenant_wide" in visible  # `tenant` admits every member


async def test_member_sees_only_fresh_mirrored_docs(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> None:
    visible = await _visible_docs(sessionmaker, world, world.member_id)
    assert visible == {"member_fresh", "tenant_wide"}


async def test_grantee_denied_on_enforced_doc_despite_grant(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> None:
    """INV-2 (ADR-0019 §2): a Lumen grant can never admit a user the source
    denies — but the same grantee's grant on a NON-enforced upload still works."""
    visible = await _visible_docs(sessionmaker, world, world.grantee_id)
    assert "empty" not in visible  # grant does not widen the enforced mode
    assert "upload" in visible  # the grant leg still applies where it should
    assert visible == {"upload", "tenant_wide"}


async def test_stale_beyond_window_and_stamped_are_denied(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> None:
    visible = await _visible_docs(sessionmaker, world, world.member_id)
    assert "member_stale" not in visible  # beyond CONNECTOR_ACL_MAX_AGE_HOURS
    assert "member_stamped" not in visible  # NULL = stale-stamped ⇒ immediate deny


async def test_hydration_recheck_drops_unpermitted_chunks(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> None:
    """The load_passages hydration step re-evaluates the SAME mode-split
    predicate: engine hits for denied documents never become passages."""
    all_chunk_ids = list(world.chunks.values())
    async with sessionmaker() as session:
        rows = await queries.load_passages(
            session, allow_set=_allow(world, world.member_id), chunk_ids=all_chunk_ids
        )
    names = {r.document_name.removesuffix(".txt") for r in rows.values()}
    assert names == {"member_fresh", "tenant_wide"}


async def test_get_document_text_denied_on_enforced_mismatch(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> None:
    """Direct fetch of a denied enforced document is None → the API's 404
    (existence non-disclosure, INV-2)."""
    async with sessionmaker() as session:
        got = await queries.load_document_text(
            session,
            allow_set=_allow(world, world.owner_id),
            document_id=world.docs["member_fresh"],
        )
    assert got is None
    async with sessionmaker() as session:
        ok = await queries.load_document_text(
            session,
            allow_set=_allow(world, world.member_id),
            document_id=world.docs["member_fresh"],
        )
    assert ok is not None


async def test_cross_tenant_enforced_doc_is_invisible(
    sessionmaker: async_sessionmaker[AsyncSession], world: _World
) -> None:
    """INV-1 sits outside the mode split: a foreign tenant's allow-set sees
    nothing even with a matching principal string."""
    async with sessionmaker() as session:
        foreign_tenant = await TenantRepository(session).create(name="Globex")
        foreign_user = await UserRepository(session, foreign_tenant.id).create(
            email="spy@globex.test", password_hash="h", roles=[Role.MEMBER]
        )
        await session.commit()
    allow = AllowSet(
        tenant_id=foreign_tenant.id,
        owner_ids=frozenset({foreign_user.id}),
        grant_principal_id=foreign_user.id,
        acl_principals=frozenset({f"user:{world.member_id}", "tenant"}),
    )
    async with sessionmaker() as session:
        rows = await queries.list_documents(session, allow_set=allow, k=50)
    assert rows == []


# --- engine filter (the OpenSearch chokepoint) --------------------------------


def _branches(clauses: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    split = clauses[1]["bool"]
    assert split["minimum_should_match"] == 1
    not_enforced, enforced = split["should"]
    return not_enforced, enforced


def test_engine_filter_mode_split_shape() -> None:
    """The engine mirror of _document_permitted: tenant term OUTSIDE the split;
    non-enforced branch = the pre-0019 owner/grant terms; enforced branch =
    principals ∩ + freshness range, and nothing else."""
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    allow = SearchAllowFilter(
        tenant_id=tenant,
        owner_ids=frozenset({owner}),
        acl_principals=frozenset({f"user:{owner}", "tenant"}),
    )
    clauses = allow.to_engine_filter(now=now)
    assert clauses[0] == {"term": {"tenant_id": str(tenant)}}  # INV-1, untouched
    not_enforced, enforced = _branches(clauses)

    assert not_enforced["bool"]["filter"][0] == {"term": {"acl_enforced": False}}
    should = not_enforced["bool"]["filter"][1]["bool"]["should"]
    assert should == [{"terms": {"owner_id": [str(owner)]}}]

    filters = enforced["bool"]["filter"]
    assert filters[0] == {"term": {"acl_enforced": True}}
    assert filters[1] == {"terms": {"acl_principals": sorted({f"user:{owner}", "tenant"})}}
    assert filters[2] == {"range": {"acl_synced_at": {"gte": acl_freshness_floor(now).isoformat()}}}
    # The enforced branch carries NO owner/grant terms (exclusive modes).
    assert len(filters) == 3


def test_engine_filter_grant_sets_only_widen_non_enforced_branch() -> None:
    tenant, owner = uuid.uuid4(), uuid.uuid4()
    doc, coll = uuid.uuid4(), uuid.uuid4()
    allow = SearchAllowFilter(
        tenant_id=tenant,
        owner_ids=frozenset({owner}),
        granted_document_ids=frozenset({doc}),
        granted_collection_ids=frozenset({coll}),
    )
    not_enforced, enforced = _branches(allow.to_engine_filter())
    should = not_enforced["bool"]["filter"][1]["bool"]["should"]
    assert {"terms": {"document_id": [str(doc)]}} in should
    assert {"terms": {"collection_id": [str(coll)]}} in should
    # Grants never appear in the enforced branch.
    assert all("document_id" not in str(f) for f in enforced["bool"]["filter"])


def test_engine_filter_empty_principal_set_admits_no_enforced_docs() -> None:
    """Defaulted acl_principals ⇒ the enforced branch's terms clause is empty —
    it can match nothing (fail closed), while the non-enforced branch works."""
    allow = SearchAllowFilter(tenant_id=uuid.uuid4(), owner_ids=frozenset({uuid.uuid4()}))
    _, enforced = _branches(allow.to_engine_filter())
    assert enforced["bool"]["filter"][1] == {"terms": {"acl_principals": []}}


def test_retrieval_allow_set_carries_mirrored_principals() -> None:
    """AllowSet.for_principal derives {user:<id>, tenant} from the token-bound
    principal — the identity both chokepoints intersect with."""
    from app.auth.principal import Principal

    principal = Principal(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        roles=(Role.MEMBER,),
    )
    allow = AllowSet.for_principal(principal)
    assert allow.acl_principals == frozenset({f"user:{principal.user_id}", "tenant"})
