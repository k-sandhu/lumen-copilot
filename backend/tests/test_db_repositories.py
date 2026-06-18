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
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
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
