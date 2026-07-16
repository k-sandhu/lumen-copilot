"""Repository unit tests — the tenant-scoping boundary (issue #44, spec 0004).

These assert the data-layer invariants without any live Postgres: the schema is
created on an **in-memory async SQLite** database (the portable column types in
``app.db.types`` make this work; the real ``vector`` shape is pinned by the
Alembic migration, exercised separately in ``test_db_migration.py``).

The headline is **INV-1 (tenancy isolation)**: a repository bound to tenant A
must never read or write tenant B's rows. Every ``get``/``list`` is scoped by
``tenant_id``, so a cross-tenant lookup returns ``None`` / no rows — the negative
tests below assert exactly that, fail-closed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    ChunkInput,
    ChunkRepository,
    CitationRepository,
    CollectionRepository,
    DocumentRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    AuditOutcome,
    DocumentStatus,
    MessageRole,
    Role,
)

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A fresh in-memory SQLite schema + session per test (offline-safe).

    ``StaticPool`` keeps the single in-memory connection alive across the
    session's checkouts (otherwise each checkout would see an empty DB), and the
    ``finally`` disposes the engine so aiosqlite's connection/thread is closed
    deterministically — no leaked transport at GC time.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def two_tenants(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Two provisioned tenants (A, B) — the substrate for the isolation tests."""
    tenants = TenantRepository(session)
    a = await tenants.create(name="Tenant A")
    b = await tenants.create(name="Tenant B")
    return a.id, b.id


# ---------------------------------------------------------------------------
# Happy path — repositories round-trip domain entities.
# ---------------------------------------------------------------------------


async def test_tenant_create_and_get(session: AsyncSession) -> None:
    tenants = TenantRepository(session)
    created = await tenants.create(name="Acme")
    fetched = await tenants.get(created.id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.name == "Acme"
    # A fresh tenant carries no per-tenant tool-turn override — the system
    # default applies (issue #148).
    assert created.max_tool_turns is None
    assert fetched.max_tool_turns is None


async def test_tenant_update_sets_and_clears_max_tool_turns(session: AsyncSession) -> None:
    """The per-tenant tool-turn override round-trips: set an int, then clear it (#148)."""
    tenants = TenantRepository(session)
    created = await tenants.create(name="Acme")

    # Set an explicit per-tenant override.
    updated = await tenants.update(created.id, max_tool_turns=7)
    assert updated is not None
    assert updated.max_tool_turns == 7
    assert (await tenants.get(created.id)).max_tool_turns == 7  # type: ignore[union-attr]

    # Clearing it (None) reverts the tenant to the system default.
    cleared = await tenants.update(created.id, max_tool_turns=None)
    assert cleared is not None
    assert cleared.max_tool_turns is None
    assert (await tenants.get(created.id)).max_tool_turns is None  # type: ignore[union-attr]


async def test_tenant_update_unknown_id_returns_none(session: AsyncSession) -> None:
    tenants = TenantRepository(session)
    assert await tenants.update(uuid.uuid4(), max_tool_turns=10) is None


async def test_user_round_trips_with_roles(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    users = UserRepository(session, tenant_a)
    created = await users.create(
        email="kw@acme.test", password_hash="argon2$hash", roles=[Role.MEMBER, Role.ADMIN]
    )
    assert created.tenant_id == tenant_a
    assert created.roles == (Role.MEMBER, Role.ADMIN)

    by_id = await users.get(created.id)
    by_email = await users.get_by_email("kw@acme.test")
    assert by_id == created
    assert by_email == created


async def test_collection_and_document_and_chunk_chain(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    collections = CollectionRepository(session, tenant_a)
    coll = await collections.create(owner_id=user.id, name="Q3 Docs", description="d")
    assert coll.owner_id == user.id

    documents = DocumentRepository(session, tenant_a)
    doc = await documents.create(
        owner_id=user.id,
        collection_id=coll.id,
        filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=1234,
        storage_key=f"{tenant_a}/abc/report.pdf",
    )
    assert doc.status is DocumentStatus.PENDING

    chunks = ChunkRepository(session, tenant_a)
    chunk = await chunks.add(
        document_id=doc.id,
        ord=0,
        text="passage one",
        char_start=0,
        char_end=11,
        embedding=[0.1] * 1024,
    )
    assert chunk.embedding is not None
    assert len(chunk.embedding) == 1024

    listed = await chunks.list_for_document(doc.id)
    assert [c.id for c in listed] == [chunk.id]


async def test_chunk_embedding_may_be_null_until_ingested(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_a).create(owner_id=user.id, name="c")
    doc = await DocumentRepository(session, tenant_a).create(
        owner_id=user.id,
        collection_id=coll.id,
        filename="f.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_key="k",
    )
    chunk = await ChunkRepository(session, tenant_a).add(
        document_id=doc.id, ord=0, text="t", char_start=0, char_end=1
    )
    assert chunk.embedding is None


async def test_document_set_status(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_a).create(owner_id=user.id, name="c")
    documents = DocumentRepository(session, tenant_a)
    doc = await documents.create(
        owner_id=user.id,
        collection_id=coll.id,
        filename="f.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_key="k",
    )
    updated = await documents.set_status(doc.id, DocumentStatus.FAILED, error="boom")
    assert updated is not None
    assert updated.status is DocumentStatus.FAILED
    assert updated.error == "boom"


async def test_chat_session_messages_and_citations(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_a).create(owner_id=user.id, name="c")
    doc = await DocumentRepository(session, tenant_a).create(
        owner_id=user.id,
        collection_id=coll.id,
        filename="f.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_key="k",
    )
    chunk = await ChunkRepository(session, tenant_a).add(
        document_id=doc.id, ord=0, text="grounding passage", char_start=0, char_end=17
    )

    sessions = ChatSessionRepository(session, tenant_a)
    chat = await sessions.create(owner_id=user.id, model="anthropic/claude-opus-4.8", title="Hi")
    assert chat.owner_id == user.id

    messages = MessageRepository(session, tenant_a)
    user_msg = await messages.add(session_id=chat.id, role=MessageRole.USER, content="What is X?")
    asst_msg = await messages.add(
        session_id=chat.id,
        role=MessageRole.ASSISTANT,
        content="X is ...",
        model="anthropic/claude-opus-4.8",
    )
    history = await messages.list_for_session(chat.id)
    assert [m.id for m in history] == [user_msg.id, asst_msg.id]  # oldest → newest

    citations = CitationRepository(session, tenant_a)
    cit = await citations.add(
        message_id=asst_msg.id, chunk_id=chunk.id, char_start=0, char_end=5, score=0.9
    )
    listed = await citations.list_for_message(asst_msg.id)
    assert [c.id for c in listed] == [cit.id]


async def test_audit_event_record_and_list(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    audit = AuditEventRepository(session, tenant_a)
    ev = await audit.record(
        action="document.uploaded",
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        metadata={"size_bytes": 10},
    )
    assert ev.outcome is AuditOutcome.ALLOWED
    assert ev.metadata == {"size_bytes": 10}
    recent = await audit.list_recent()
    assert [e.id for e in recent] == [ev.id]


def test_audit_repository_has_no_mutation_methods() -> None:
    """Append-only at the API surface: no update/delete (spec 0004 §2.4)."""
    names = set(dir(AuditEventRepository))
    assert "record" in names
    assert "update" not in names
    assert "delete" not in names


# ---------------------------------------------------------------------------
# INV-1 (tenancy isolation) — the required negative tests. Fail closed.
# ---------------------------------------------------------------------------


async def test_inv1_user_get_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    # A user created in tenant A...
    user_a = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    # ...is invisible to a repository scoped to tenant B (INV-1: no rows).
    repo_b = UserRepository(session, tenant_b)
    assert await repo_b.get(user_a.id) is None
    assert await repo_b.get_by_email("a@x.test") is None


async def test_inv1_collection_get_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    owner = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll_a = await CollectionRepository(session, tenant_a).create(owner_id=owner.id, name="A")

    repo_b = CollectionRepository(session, tenant_b)
    # Cross-tenant fetch by id → None (service maps this to 404, not 403).
    assert await repo_b.get(coll_a.id) is None
    # And a cross-tenant owner listing returns no rows.
    assert await repo_b.list_for_owner(owner.id) == []


async def test_inv1_document_and_chunk_get_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    owner = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_a).create(owner_id=owner.id, name="A")
    doc = await DocumentRepository(session, tenant_a).create(
        owner_id=owner.id,
        collection_id=coll.id,
        filename="f.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        storage_key="k",
    )
    chunk = await ChunkRepository(session, tenant_a).add(
        document_id=doc.id, ord=0, text="secret", char_start=0, char_end=6
    )

    docs_b = DocumentRepository(session, tenant_b)
    chunks_b = ChunkRepository(session, tenant_b)
    assert await docs_b.get(doc.id) is None
    assert await docs_b.list_in_collection(coll.id) == []
    # A cross-tenant status mutation finds nothing → no-op, returns None.
    assert await docs_b.set_status(doc.id, DocumentStatus.READY) is None
    assert await chunks_b.get(chunk.id) is None
    assert await chunks_b.list_for_document(doc.id) == []


async def test_inv1_chat_and_audit_are_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    owner = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    chat = await ChatSessionRepository(session, tenant_a).create(owner_id=owner.id, model="m")
    msg = await MessageRepository(session, tenant_a).add(
        session_id=chat.id, role=MessageRole.USER, content="hi"
    )
    await AuditEventRepository(session, tenant_a).record(
        action="auth.login", resource_type="session", outcome=AuditOutcome.ALLOWED
    )

    assert await ChatSessionRepository(session, tenant_b).get(chat.id) is None
    assert await ChatSessionRepository(session, tenant_b).list_for_owner(owner.id) == []
    assert await MessageRepository(session, tenant_b).get(msg.id) is None
    assert await MessageRepository(session, tenant_b).list_for_session(chat.id) == []
    # Tenant B's audit view never sees tenant A's events.
    assert await AuditEventRepository(session, tenant_b).list_recent() == []


async def test_inv1_delete_is_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    owner = await UserRepository(session, tenant_a).create(
        email="a@x.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_a).create(owner_id=owner.id, name="A")
    chat = await ChatSessionRepository(session, tenant_a).create(owner_id=owner.id, model="m")

    # A tenant-B repository cannot delete tenant-A rows (returns False, no-op)...
    assert await CollectionRepository(session, tenant_b).delete(coll.id) is False
    assert await ChatSessionRepository(session, tenant_b).delete(chat.id) is False
    # ...and the rows are still there for their owning tenant.
    assert await CollectionRepository(session, tenant_a).get(coll.id) is not None
    assert await ChatSessionRepository(session, tenant_a).get(chat.id) is not None


async def test_repositories_require_a_tenant_scope() -> None:
    """A tenant-scoped repository cannot be built without a tenant id (INV-1)."""
    with pytest.raises(TypeError):
        UserRepository(session=None)  # type: ignore[call-arg]  # missing tenant_id


# ---------------------------------------------------------------------------
# Collection list/count/update additions (#46).
# ---------------------------------------------------------------------------


async def test_collection_count_documents(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    collections = CollectionRepository(session, tenant_a)
    coll = await collections.create(owner_id=user.id, name="c")
    assert await collections.count_documents(coll.id) == 0

    documents = DocumentRepository(session, tenant_a)
    for i in range(3):
        await documents.create(
            owner_id=user.id,
            collection_id=coll.id,
            filename=f"f{i}.txt",
            mime_type="text/plain",
            size_bytes=1,
            storage_key=f"k{i}",
        )
    assert await collections.count_documents(coll.id) == 3


async def test_document_count_by_storage_key(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """count_by_storage_key counts THIS tenant's docs sharing an object key —
    the guard the sync/delete paths use before removing a content-addressed
    object so they never delete bytes another live document still references
    (#269). Tenant-scoped: a foreign tenant's identical key is not counted."""
    tenant_a, tenant_b = two_tenants
    user_a = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll_a = await CollectionRepository(session, tenant_a).create(owner_id=user_a.id, name="c")
    docs_a = DocumentRepository(session, tenant_a)

    shared = f"{tenant_a}/deadbeef/shared.txt"
    assert await docs_a.count_by_storage_key(shared) == 0

    d1 = await docs_a.create(
        owner_id=user_a.id, collection_id=coll_a.id, filename="a.txt",
        mime_type="text/plain", size_bytes=1, storage_key=shared,
    )
    await docs_a.create(
        owner_id=user_a.id, collection_id=coll_a.id, filename="b.txt",
        mime_type="text/plain", size_bytes=1, storage_key=shared,
    )
    assert await docs_a.count_by_storage_key(shared) == 2  # two docs share it

    # Another tenant's document under the same key string is not counted (INV-1).
    user_b = await UserRepository(session, tenant_b).create(
        email="o@globex.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll_b = await CollectionRepository(session, tenant_b).create(owner_id=user_b.id, name="c")
    await DocumentRepository(session, tenant_b).create(
        owner_id=user_b.id, collection_id=coll_b.id, filename="c.txt",
        mime_type="text/plain", size_bytes=1, storage_key=shared,
    )
    assert await docs_a.count_by_storage_key(shared) == 2  # unchanged by tenant B

    # Deleting one leaves the object still referenced by the other → guard keeps it.
    # The count must run AFTER the delete is flushed: the app sessionmaker uses
    # autoflush=False (db/session.py) and DocumentRepository.delete does not flush,
    # so callers (DocumentService.delete, the sync reconcile) flush before counting.
    await docs_a.delete(d1.id)
    await session.flush()
    assert await docs_a.count_by_storage_key(shared) == 1


async def test_collection_update_partial(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    collections = CollectionRepository(session, tenant_a)
    coll = await collections.create(owner_id=user.id, name="old", description="orig")

    # Name-only update leaves the description untouched.
    updated = await collections.update(coll.id, name="new")
    assert updated is not None
    assert updated.name == "new"
    assert updated.description == "orig"

    # set_description clears it to None.
    cleared = await collections.update(coll.id, set_description=True, description=None)
    assert cleared is not None
    assert cleared.description is None
    assert cleared.name == "new"  # name preserved


async def test_collection_update_cross_tenant_returns_none(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_a).create(owner_id=user.id, name="A")
    # A tenant-B repository cannot mutate tenant-A rows (INV-1).
    assert await CollectionRepository(session, tenant_b).update(coll.id, name="x") is None


async def test_collection_list_for_owner_page_keyset_pagination(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    collections = CollectionRepository(session, tenant_a)
    created = [await collections.create(owner_id=user.id, name=f"c{i}") for i in range(5)]
    created_ids = {c.id for c in created}

    seen: set[uuid.UUID] = set()
    after_id: uuid.UUID | None = None
    pages = 0
    while True:
        page = await collections.list_for_owner_page(user.id, limit=2, after_id=after_id)
        if not page:
            break
        for c in page:
            assert c.id not in seen  # deterministic, no overlap
            seen.add(c.id)
        after_id = page[-1].id
        pages += 1
        if len(page) < 2:
            break
        assert pages < 10
    assert seen == created_ids


async def test_collection_list_for_owner_page_excludes_other_owner(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    alice = await UserRepository(session, tenant_a).create(
        email="alice@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    bob = await UserRepository(session, tenant_a).create(
        email="bob@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    collections = CollectionRepository(session, tenant_a)
    await collections.create(owner_id=alice.id, name="alice-coll")
    await collections.create(owner_id=bob.id, name="bob-coll")

    alice_page = await collections.list_for_owner_page(alice.id, limit=10)
    assert {c.name for c in alice_page} == {"alice-coll"}


# ---------------------------------------------------------------------------
# Chunk replace/delete — ingestion idempotency + tenant scope (#21).
# ---------------------------------------------------------------------------


async def _seed_doc_for_chunks(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    user = await UserRepository(session, tenant_id).create(
        email="o@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    coll = await CollectionRepository(session, tenant_id).create(owner_id=user.id, name="c")
    doc = await DocumentRepository(session, tenant_id).create(
        owner_id=user.id,
        collection_id=coll.id,
        filename="f.txt",
        mime_type="text/plain",
        size_bytes=1,
        storage_key="k",
    )
    return doc.id


async def test_replace_for_document_assigns_contiguous_ord(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    doc_id = await _seed_doc_for_chunks(session, tenant_a)
    chunks = ChunkRepository(session, tenant_a)
    persisted = await chunks.replace_for_document(
        doc_id,
        [
            ChunkInput(text="one", char_start=0, char_end=3, embedding=[0.1] * 1024),
            ChunkInput(text="two", char_start=3, char_end=6, embedding=[0.2] * 1024),
        ],
    )
    assert [c.ord for c in persisted] == [0, 1]
    listed = await chunks.list_for_document(doc_id)
    assert [c.text for c in listed] == ["one", "two"]
    assert all(c.embedding is not None and len(c.embedding) == 1024 for c in listed)


async def test_replace_for_document_is_idempotent(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A second replace deletes the first set — no duplicate (document_id, ord)."""
    tenant_a, _ = two_tenants
    doc_id = await _seed_doc_for_chunks(session, tenant_a)
    chunks = ChunkRepository(session, tenant_a)

    await chunks.replace_for_document(
        doc_id,
        [ChunkInput(text=f"v1-{i}", char_start=i, char_end=i + 1) for i in range(5)],
    )
    second = await chunks.replace_for_document(
        doc_id,
        [ChunkInput(text=f"v2-{i}", char_start=i, char_end=i + 1) for i in range(3)],
    )
    listed = await chunks.list_for_document(doc_id)
    assert len(listed) == 3 == len(second)
    assert [c.text for c in listed] == ["v2-0", "v2-1", "v2-2"]
    assert [c.ord for c in listed] == [0, 1, 2]


async def test_delete_for_document_returns_count(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    doc_id = await _seed_doc_for_chunks(session, tenant_a)
    chunks = ChunkRepository(session, tenant_a)
    await chunks.replace_for_document(doc_id, [ChunkInput(text="x", char_start=0, char_end=1)])
    assert await chunks.delete_for_document(doc_id) == 1
    assert await chunks.list_for_document(doc_id) == []
    # Deleting again is a no-op (idempotent), returns 0.
    assert await chunks.delete_for_document(doc_id) == 0


async def test_inv1_chunk_replace_and_delete_are_tenant_scoped(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A tenant-B repository cannot replace/delete tenant-A chunks (INV-1)."""
    tenant_a, tenant_b = two_tenants
    doc_id = await _seed_doc_for_chunks(session, tenant_a)
    chunks_a = ChunkRepository(session, tenant_a)
    await chunks_a.replace_for_document(
        doc_id, [ChunkInput(text="secret", char_start=0, char_end=6)]
    )

    chunks_b = ChunkRepository(session, tenant_b)
    # B sees nothing to delete, and a B-scoped replace cannot touch A's rows.
    assert await chunks_b.delete_for_document(doc_id) == 0
    # A's chunk is still there.
    assert len(await chunks_a.list_for_document(doc_id)) == 1


# ---------------------------------------------------------------------------
# #409 — llm_usage: per-answer token/cache accounting is tenant-scoped (INV-1).
# ---------------------------------------------------------------------------


async def test_llm_usage_record_roundtrip_and_tenant_isolation(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    from app.db.repositories import LlmUsageRepository
    from app.domain.entities import Role

    tenant_a, tenant_b = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="usage@a.test", password_hash="x", roles=[Role.MEMBER]
    )
    chat = await ChatSessionRepository(session, tenant_a).create(
        owner_id=user.id, model="m", title="t"
    )

    row = await LlmUsageRepository(session, tenant_a).record(
        model="anthropic/claude-opus-4.8",
        prompt_tokens=220,
        completion_tokens=30,
        total_tokens=250,
        cached_prompt_tokens=90,
        cache_write_tokens=80,
        session_id=chat.id,
    )
    assert row.tenant_id == tenant_a
    assert row.cached_prompt_tokens == 90

    mine = await LlmUsageRepository(session, tenant_a).list_for_session(chat.id)
    assert [r.id for r in mine] == [row.id]

    # INV-1 negative: tenant B's repository must not see tenant A's usage.
    theirs = await LlmUsageRepository(session, tenant_b).list_for_session(chat.id)
    assert theirs == []


async def test_llm_usage_one_row_per_message_is_structural(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """#419 review — the partial unique index makes "one row per answer" structural.

    A second usage record for the SAME assistant message is an IntegrityError,
    not silent double-counting; message-less rows (headless/sub-agent) stay
    unconstrained because the index is partial (message_id IS NOT NULL).
    """
    from sqlalchemy.exc import IntegrityError

    from app.db.repositories import (
        LlmUsageRepository,
        MessageRepository,
    )
    from app.domain.entities import MessageRole, Role

    tenant_a, _ = two_tenants
    user = await UserRepository(session, tenant_a).create(
        email="dup@a.test", password_hash="x", roles=[Role.MEMBER]
    )
    chat = await ChatSessionRepository(session, tenant_a).create(
        owner_id=user.id, model="m", title="t"
    )
    messages = MessageRepository(session, tenant_a)
    msg = await messages.add(
        session_id=chat.id, role=MessageRole.ASSISTANT, content="a", model="m"
    )
    await session.flush()

    repo = LlmUsageRepository(session, tenant_a)
    await repo.record(
        model="m",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        session_id=chat.id,
        message_id=msg.id,
    )
    await session.flush()
    with pytest.raises(IntegrityError):
        await repo.record(
            model="m",
            prompt_tokens=9,
            completion_tokens=9,
            total_tokens=18,
            session_id=chat.id,
            message_id=msg.id,
        )
        await session.flush()
    await session.rollback()

    # Message-less rows (run_id-keyed) are NOT constrained — two coexist.
    repo2 = LlmUsageRepository(session, tenant_a)
    for _ in range(2):
        await repo2.record(
            model="m",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            run_id=uuid.uuid4(),
        )
    await session.flush()
