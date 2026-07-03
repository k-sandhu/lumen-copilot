"""Explicit ACL grants — the widened retrieval filter + the grant service (#18).

The point of CC-1 / INV-2 (spec 0004 §2.2): sharing is **explicit**, via the
``grants`` table, and a grant must make the resource retrievable *and* citable by
the grantee through the **one** retrieval chokepoint — while deny-by-default
holds for everything un-granted.

Two layers, both honouring the offline-safe pattern of the other retrieval tests:

* **Offline (in-memory SQLite)** — ``search_documents`` and ``get_document`` issue
  plain relational SQL (``ilike`` / ordered selects / the grant ``EXISTS``), which
  runs on SQLite, so the **widened permission filter** (owner OR explicit grant,
  with the collection-grant cascade) is fully exercised here without Postgres. The
  ``GrantsService`` authorization (owner-or-admin, 404 on cross-tenant / non-owned)
  and its audit emission are also exercised offline.
* **Live (compose Postgres + pgvector)** — the hybrid ``search`` (semantic +
  lexical legs) uses pgvector / full-text, which only exist on Postgres; that path
  asserts a *granted* document is returned by the fused hybrid search too. Skips
  automatically when Postgres is unreachable.

Headlines (INV-2):

* positive — a document/collection granted to user B becomes retrievable AND
  citable by B (document_search + get_document offline; hybrid search live);
* negative — a non-granted doc is excluded for B; a **revoked** grant excludes it
  again; a grant in tenant A is invisible in tenant B (cross-tenant grant denied);
  the owner still sees their own throughout.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.errors import NotFoundError, ValidationError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import (
    DocumentStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    Role,
)
from app.domain.llm import Embedding
from app.retrieval import RetrievalService
from app.services.audit import AuditSink
from app.services.grants_service import GrantsService

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip

_EMBED_DIM = 1024


class _FakeGateway:
    """No-network stand-in for the #36 gateway's ``embed`` (fixed vector)."""

    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector if vector is not None else [0.0] * _EMBED_DIM

    async def embed(self, inputs: list[str]) -> list[Embedding]:
        return [Embedding(vector=list(self._vector), model="fake-bge-m3") for _ in inputs]


def _principal(
    user_id: uuid.UUID, tenant_id: uuid.UUID, *, roles: tuple[Role, ...] = (Role.MEMBER,)
) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=roles)


# ---------------------------------------------------------------------------
# Offline fixtures: in-memory SQLite schema + two tenants.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """A fresh in-memory SQLite schema + session per test (offline-safe)."""
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
    tenants = TenantRepository(session)
    a = await tenants.create(name="Tenant A")
    b = await tenants.create(name="Tenant B")
    return a.id, b.id


async def _make_user(session: AsyncSession, tenant_id: uuid.UUID, email: str) -> uuid.UUID:
    user = await UserRepository(session, tenant_id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    return user.id


async def _make_document(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    filename: str,
    chunk_texts: list[str],
    collection_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a collection (unless given) + a doc with chunks → (collection_id, doc_id)."""
    if collection_id is None:
        coll = await CollectionRepository(session, tenant_id).create(owner_id=owner_id, name="C")
        collection_id = coll.id
    doc = await DocumentRepository(session, tenant_id).create(
        owner_id=owner_id,
        collection_id=collection_id,
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
    await ChunkRepository(session, tenant_id).replace_for_document(doc.id, inputs)
    return collection_id, doc.id


def _grants_service(
    session: AsyncSession, *, tenant_id: uuid.UUID, owner_id: uuid.UUID, roles: tuple[Role, ...]
) -> GrantsService:
    audit = AuditSink(AuditEventRepository(session, tenant_id))
    return GrantsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        audit=audit,
        request_id="req-test",
        source_ip="203.0.113.1",
    )


# ---------------------------------------------------------------------------
# Positive: a granted document is retrievable + citable by the grantee (offline).
# ---------------------------------------------------------------------------


async def test_document_grant_makes_doc_retrievable_by_grantee(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-2 positive: a document granted to user B is retrievable + citable by B.

    Before the grant, B's ``search_documents`` / ``get_document`` exclude A's doc
    (deny-by-default); after an explicit document grant to B, both return it — and
    ``get_document`` yields the full text (citable provenance).
    """
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    _coll_a, doc_a = await _make_document(
        session,
        tenant_id=tenant_a,
        owner_id=user_a,
        filename="q3-budget.txt",
        chunk_texts=["the ", "quarterly budget"],
    )
    svc = RetrievalService(session, gateway=_FakeGateway())

    # Before the grant: B sees nothing (deny-by-default).
    assert (
        await svc.search_documents(principal=_principal(user_b, tenant_a), name_or_query="budget")
        == []
    )
    assert await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc_a) is None

    # A grants B viewer access to the document.
    await _grants_service(
        session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,)
    ).create_grant(
        resource_type=GrantResourceType.DOCUMENT,
        resource_id=doc_a,
        principal_id=user_b,
    )

    # After the grant: B retrieves + can cite (full text reassembled).
    hits = await svc.search_documents(
        principal=_principal(user_b, tenant_a), name_or_query="budget"
    )
    assert [h.document_id for h in hits] == [doc_a]
    text = await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc_a)
    assert text is not None
    assert text.text == "the quarterly budget"
    # The owner still sees their own throughout (the grant did not remove ownership).
    own = await svc.search_documents(principal=_principal(user_a, tenant_a), name_or_query="budget")
    assert [h.document_id for h in own] == [doc_a]


async def test_collection_grant_cascades_to_its_documents(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-2 positive: a **collection** grant cascades to every document in it.

    A grant on the collection (not the individual documents) makes *all* of the
    collection's documents retrievable + citable by the grantee — the cascade the
    filter implements via ``grants.resource_id = documents.collection_id``.
    """
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    coll_a, doc1 = await _make_document(
        session,
        tenant_id=tenant_a,
        owner_id=user_a,
        filename="budget-one.txt",
        chunk_texts=["budget one"],
    )
    _coll, doc2 = await _make_document(
        session,
        tenant_id=tenant_a,
        owner_id=user_a,
        filename="budget-two.txt",
        chunk_texts=["budget two"],
        collection_id=coll_a,  # same collection
    )
    svc = RetrievalService(session, gateway=_FakeGateway())

    await _grants_service(
        session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,)
    ).create_grant(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=coll_a,
        principal_id=user_b,
    )

    hits = await svc.search_documents(
        principal=_principal(user_b, tenant_a), name_or_query="budget"
    )
    assert {h.document_id for h in hits} == {doc1, doc2}
    # Both documents are individually fetchable (citable) via the cascade.
    assert (
        await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc1) is not None
    )
    assert (
        await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc2) is not None
    )


# ---------------------------------------------------------------------------
# Negative: un-granted excluded; revoked excludes again; cross-tenant denied.
# ---------------------------------------------------------------------------


async def test_non_granted_document_excluded_for_other_user(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-2 negative: with no grant, B never sees A's document (deny-by-default)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    _coll, doc_a = await _make_document(
        session,
        tenant_id=tenant_a,
        owner_id=user_a,
        filename="secret.txt",
        chunk_texts=["secret"],
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    assert (
        await svc.search_documents(principal=_principal(user_b, tenant_a), name_or_query="secret")
        == []
    )
    assert await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc_a) is None


async def test_revoked_grant_excludes_document_again(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-2 negative: a **revoked** grant excludes the document again (deny restored)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    _coll, doc_a = await _make_document(
        session,
        tenant_id=tenant_a,
        owner_id=user_a,
        filename="report.txt",
        chunk_texts=["report body"],
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    grants = _grants_service(session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,))

    await grants.create_grant(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_b
    )
    # Granted → visible.
    assert (
        await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc_a)
        is not None
    )

    revoked = await grants.revoke_grant(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_b
    )
    assert revoked is True
    # Revoked → excluded again.
    assert await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc_a) is None
    assert (
        await svc.search_documents(principal=_principal(user_b, tenant_a), name_or_query="report")
        == []
    )


async def test_cross_tenant_grant_is_invisible(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1/INV-2 negative: a grant minted in tenant A cannot widen tenant B.

    A grant row whose ``tenant_id`` is tenant A is invisible to a tenant-B
    retrieval — the filter binds the requester's tenant, so a same-id principal in
    tenant B is not admitted by tenant A's grant. (Cross-tenant resources never
    share a tenant_id; this asserts the grant leg is tenant-scoped too.)
    """
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    # A user in tenant B that we will (illegitimately) try to make see A's doc.
    user_b = await _make_user(session, tenant_b, "b@y.test")
    _coll, doc_a = await _make_document(
        session,
        tenant_id=tenant_a,
        owner_id=user_a,
        filename="a-only.txt",
        chunk_texts=["a only"],
    )
    # A grant on A's document to a principal id equal to user_b — but recorded in
    # tenant A. A tenant-B principal must still not see A's document.
    await GrantRepository(session, tenant_a).create(
        resource_type=GrantResourceType.DOCUMENT,
        resource_id=doc_a,
        principal_type=GrantPrincipalType.USER,
        principal_id=user_b,
        role=GrantRole.VIEWER,
        granted_by=user_a,
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    # user_b is in tenant B; the document and grant are in tenant A → excluded.
    assert await svc.get_document(principal=_principal(user_b, tenant_b), document_id=doc_a) is None
    assert (
        await svc.search_documents(principal=_principal(user_b, tenant_b), name_or_query="a-only")
        == []
    )


# ---------------------------------------------------------------------------
# GrantsService authorization (owner-or-admin; 404 on non-owned / cross-tenant).
# ---------------------------------------------------------------------------


async def test_only_owner_may_grant(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A non-owner, non-admin granting on someone else's resource → 404 (deny)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    user_c = await _make_user(session, tenant_a, "c@x.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["a"]
    )
    # user_b (not the owner, not admin) tries to grant user_c access to A's doc.
    grants_b = _grants_service(session, tenant_id=tenant_a, owner_id=user_b, roles=(Role.MEMBER,))
    with pytest.raises(NotFoundError):
        await grants_b.create_grant(
            resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_c
        )
    # No grant was written → C still cannot see A's doc.
    svc = RetrievalService(session, gateway=_FakeGateway())
    assert await svc.get_document(principal=_principal(user_c, tenant_a), document_id=doc_a) is None


async def test_tenant_admin_may_grant_any_resource(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A tenant admin may grant on a resource they do not personally own (spec §2.2/§2.3)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    admin = await _make_user(session, tenant_a, "admin@x.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["alpha"]
    )
    grants_admin = _grants_service(
        session, tenant_id=tenant_a, owner_id=admin, roles=(Role.MEMBER, Role.ADMIN)
    )
    await grants_admin.create_grant(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_b
    )
    svc = RetrievalService(session, gateway=_FakeGateway())
    assert (
        await svc.get_document(principal=_principal(user_b, tenant_a), document_id=doc_a)
        is not None
    )


async def test_grant_on_foreign_tenant_resource_is_404(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Granting on a resource in another tenant → 404 (existence non-disclosure)."""
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_b, "b@y.test")
    _coll, doc_b = await _make_document(
        session, tenant_id=tenant_b, owner_id=user_b, filename="b.txt", chunk_texts=["b"]
    )
    # user_a (tenant A) tries to grant on a tenant-B document → not found.
    grants_a = _grants_service(session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,))
    with pytest.raises(NotFoundError):
        await grants_a.create_grant(
            resource_type=GrantResourceType.DOCUMENT, resource_id=doc_b, principal_id=user_a
        )


# ---------------------------------------------------------------------------
# Grantee validation: the migration has no FK on grants.principal_id, so the
# service is the only guard. A foreign-tenant / unknown grantee → 404 (+ denied
# audit, no grant row); an unsupported principal type → rejected (no grant row).
# ---------------------------------------------------------------------------


async def test_grant_to_foreign_tenant_user_is_404_with_denied_audit_and_no_row(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1/INV-2: granting to a tenant-B user from tenant A → 404, audited denied, no row.

    The owner legitimately owns the resource in their own tenant, but the grantee
    id belongs to **another tenant**. The tenant-scoped ``UserRepository`` returns
    ``None`` for that id (indistinguishable from non-existent), so the grant is
    refused as 404 (existence non-disclosure, never 403), a ``permission.denied``
    audit event is emitted, and **no grant row** is written — closing the hole
    where an owner could mint a grant that crosses the tenant boundary.
    """
    tenant_a, tenant_b = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    foreign_user = await _make_user(session, tenant_b, "foreign@y.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["a"]
    )
    grants_a = _grants_service(session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,))
    with pytest.raises(NotFoundError):
        await grants_a.create_grant(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=doc_a,
            principal_id=foreign_user,  # a real user, but in tenant B
        )
    # No grant row was written.
    stored = await GrantRepository(session, tenant_a).list_for_resource(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a
    )
    assert stored == []
    # A denied audit was emitted; no success audit.
    actions = {
        e.action for e in await AuditEventRepository(session, tenant_a).list_recent(limit=10)
    }
    assert AuditAction.PERMISSION_DENIED.value in actions
    assert AuditAction.PERMISSION_GRANTED.value not in actions
    # The foreign-tenant user still cannot see A's document (the grant never landed).
    svc = RetrievalService(session, gateway=_FakeGateway())
    assert (
        await svc.get_document(principal=_principal(foreign_user, tenant_b), document_id=doc_a)
        is None
    )


async def test_grant_to_nonexistent_user_is_404_with_no_row(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A grant to a random, non-existent user id → 404, ``permission.denied``, no row."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["a"]
    )
    grants_a = _grants_service(session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,))
    with pytest.raises(NotFoundError):
        await grants_a.create_grant(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=doc_a,
            principal_id=uuid.uuid4(),  # nobody
        )
    stored = await GrantRepository(session, tenant_a).list_for_resource(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a
    )
    assert stored == []
    actions = {
        e.action for e in await AuditEventRepository(session, tenant_a).list_recent(limit=10)
    }
    assert AuditAction.PERMISSION_DENIED.value in actions
    assert AuditAction.PERMISSION_GRANTED.value not in actions


@pytest.mark.parametrize(
    "principal_type",
    [GrantPrincipalType.GROUP, GrantPrincipalType.ROLE],
)
async def test_grant_with_unsupported_principal_type_is_rejected_no_row(
    session: AsyncSession,
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    principal_type: GrantPrincipalType,
) -> None:
    """INV-8: a ``group``/``role`` grant is rejected at the boundary (422), no row.

    The MVP supports ``user`` grants only (spec 0004 §2.2). A ``group``/``role``
    principal — which the retrieval filter never honors — is refused with a
    :class:`ValidationError` (422) before anything is persisted, and **no grant
    row** is written. (A denied audit is not emitted: this is malformed input, not
    an access denial.)
    """
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["a"]
    )
    grants_a = _grants_service(session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,))
    with pytest.raises(ValidationError):
        await grants_a.create_grant(
            resource_type=GrantResourceType.DOCUMENT,
            resource_id=doc_a,
            principal_id=user_b,
            principal_type=principal_type,
        )
    stored = await GrantRepository(session, tenant_a).list_for_resource(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a
    )
    assert stored == []


async def test_grant_emits_audit_event(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-6: a successful grant emits a ``permission.granted`` audit event."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["a"]
    )
    await _grants_service(
        session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,)
    ).create_grant(resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_b)
    events = await AuditEventRepository(session, tenant_a).list_recent(limit=10)
    actions = {e.action for e in events}
    assert AuditAction.PERMISSION_GRANTED.value in actions


async def test_denied_grant_emits_permission_denied_audit(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-6: a denied grant attempt emits a ``permission.denied`` audit event."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    user_c = await _make_user(session, tenant_a, "c@x.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["a"]
    )
    grants_b = _grants_service(session, tenant_id=tenant_a, owner_id=user_b, roles=(Role.MEMBER,))
    with pytest.raises(NotFoundError):
        await grants_b.create_grant(
            resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_c
        )
    events = await AuditEventRepository(session, tenant_a).list_recent(limit=10)
    assert AuditAction.PERMISSION_DENIED.value in {e.action for e in events}


async def test_create_grant_is_idempotent(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Re-granting the same (resource, principal) returns the same row (no duplicate)."""
    tenant_a, _ = two_tenants
    user_a = await _make_user(session, tenant_a, "a@x.test")
    user_b = await _make_user(session, tenant_a, "b@x.test")
    _coll, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=user_a, filename="a.txt", chunk_texts=["a"]
    )
    grants = _grants_service(session, tenant_id=tenant_a, owner_id=user_a, roles=(Role.MEMBER,))
    g1 = await grants.create_grant(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_b
    )
    g2 = await grants.create_grant(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a, principal_id=user_b
    )
    assert g1.id == g2.id
    stored = await GrantRepository(session, tenant_a).list_for_resource(
        resource_type=GrantResourceType.DOCUMENT, resource_id=doc_a
    )
    assert len(stored) == 1


# ---------------------------------------------------------------------------
# Live hybrid search: a granted document is returned by the fused hybrid search.
# ---------------------------------------------------------------------------

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
        "live grant-retrieval test skipped (offline-safe). The engine-backed path "
        "(ADR-0010) runs only here."
    ),
)


def _unit_vector(dim: int, hot: int) -> list[float]:
    v = [0.0] * dim
    v[hot % dim] = 1.0
    return v


@_live
async def test_live_hybrid_search_honors_grant() -> None:
    """End-to-end on Postgres + OpenSearch: a granted document is retrievable — and
    a revoked one excluded again.

    User A owns a document whose chunk text + embedding match the query, indexed
    into a per-test engine index. User B (same tenant, non-owner): before any
    grant, the engine-backed ``search`` returns nothing (the per-request grant
    id-sets are empty); after an explicit grant to B it returns A's passage with
    source + offsets (citable); after revoke it returns nothing again — the
    id-sets are resolved fresh every request, so deny-by-default is restored
    without any index change (ADR-0010 §4).
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine as _create

    from app.search import IndexedChunk, OpenSearchStore

    engine = _create(_PG_URL)
    schema = f"grant_test_{uuid.uuid4().hex[:8]}"
    store = OpenSearchStore(
        base_url=_OS_URL,
        index=f"lumen-test-{uuid.uuid4().hex[:8]}",
        dimensions=_EMBED_DIM,
        timeout_seconds=30.0,
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            await sess.execute(sql_text(f'SET search_path TO "{schema}", public'))

            tenant = (await TenantRepository(sess).create(name="A")).id
            hot = 7
            query_text = "annual revenue growth strategy"

            user_a = await UserRepository(sess, tenant).create(
                email="a@x.test", password_hash="h", roles=[Role.MEMBER]
            )
            user_b = await UserRepository(sess, tenant).create(
                email="b@x.test", password_hash="h", roles=[Role.MEMBER]
            )
            coll_a = await CollectionRepository(sess, tenant).create(owner_id=user_a.id, name="C")
            doc_a = await DocumentRepository(sess, tenant).create(
                owner_id=user_a.id,
                collection_id=coll_a.id,
                filename="a.txt",
                mime_type="text/plain",
                size_bytes=1,
                storage_key="k",
                status=DocumentStatus.READY,
            )
            chunks_a = await ChunkRepository(sess, tenant).replace_for_document(
                doc_a.id,
                [
                    ChunkInput(
                        text="annual revenue growth strategy for the year",
                        char_start=0,
                        char_end=43,
                        embedding=_unit_vector(_EMBED_DIM, hot),
                    )
                ],
            )
            await sess.commit()

            await store.ensure_index()
            await store.upsert_chunks(
                [
                    IndexedChunk(
                        chunk_id=c.id,
                        tenant_id=c.tenant_id,
                        document_id=c.document_id,
                        owner_id=user_a.id,
                        collection_id=coll_a.id,
                        ord=c.ord,
                        text=c.text,
                        embedding=c.embedding,
                        char_start=c.char_start,
                        char_end=c.char_end,
                    )
                    for c in chunks_a
                ],
                refresh=True,
            )

            gateway = _FakeGateway(_unit_vector(_EMBED_DIM, hot))
            svc = RetrievalService(sess, gateway=gateway, store=store)

            # Before the grant: B's engine-backed search returns nothing.
            before = await svc.search(
                principal=_principal(user_b.id, tenant), query=query_text, k=5
            )
            assert before == []

            # Grant B viewer access to A's document.
            audit = AuditSink(AuditEventRepository(sess, tenant))
            grants = GrantsService(
                sess,
                tenant_id=tenant,
                owner_id=user_a.id,
                roles=(Role.MEMBER,),
                audit=audit,
                request_id="req-live",
                source_ip="203.0.113.9",
            )
            await grants.create_grant(
                resource_type=GrantResourceType.DOCUMENT,
                resource_id=doc_a.id,
                principal_id=user_b.id,
            )
            await sess.commit()

            # After the grant: B's search returns A's passage, citable.
            after = await svc.search(principal=_principal(user_b.id, tenant), query=query_text, k=5)
            assert [p.document_id for p in after] == [doc_a.id]
            assert after[0].document_name == "a.txt"
            assert after[0].char_start == 0 and after[0].char_end == 43

            # Revoke → excluded again, with NO index change (deny-by-default).
            await grants.revoke_grant(
                resource_type=GrantResourceType.DOCUMENT,
                resource_id=doc_a.id,
                principal_id=user_b.id,
            )
            await sess.commit()
            revoked = await svc.search(
                principal=_principal(user_b.id, tenant), query=query_text, k=5
            )
            assert revoked == []
            # The owner is unaffected throughout.
            own = await svc.search(principal=_principal(user_a.id, tenant), query=query_text, k=5)
            assert [p.document_id for p in own] == [doc_a.id]
    finally:
        import httpx as _httpx

        async with _httpx.AsyncClient(base_url=_OS_URL, timeout=15.0) as cleanup:
            await cleanup.delete(f"/{store._index}")  # noqa: SLF001 — test teardown
            await cleanup.delete(f"/_search/pipeline/{store._index}-hybrid")  # noqa: SLF001
        await store.aclose()
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@_live
async def test_live_agent_tools_share_owner_or_grant_path() -> None:
    """The three agent tools agree on owner-or-grant (ADR-0010 slice 4, #192).

    ``search_text`` runs on OpenSearch; ``search_documents`` / ``get_document``
    stay relational. This proves all three apply the **identical** permission
    predicate — a document owned by the caller **or** granted to them, directly
    **or via a grant on its collection** (the cascade).

    Owner A has three matching documents, one per collection:

    * ``doc1`` — DOCUMENT-granted to B;
    * ``doc2`` — its collection COLLECTION-granted to B (cascade);
    * ``doc3`` — not granted.

    For grantee B, every tool returns exactly {doc1, doc2} and never doc3; the
    owner sees all three; a foreign-tenant user sees none — so the agent surface
    can never disagree about what B may read (the #181 consistency, across all
    three tools and both grant kinds).
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine as _create

    from app.search import IndexedChunk, OpenSearchStore

    engine = _create(_PG_URL)
    schema = f"tools_test_{uuid.uuid4().hex[:8]}"
    store = OpenSearchStore(
        base_url=_OS_URL,
        index=f"lumen-test-{uuid.uuid4().hex[:8]}",
        dimensions=_EMBED_DIM,
        timeout_seconds=30.0,
    )
    hot = 9
    query_text = "quarterly revenue plan"
    chunk_body = "quarterly revenue plan for the fiscal year"
    try:
        async with engine.begin() as conn:
            await conn.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.execute(sql_text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(sql_text(f'SET search_path TO "{schema}", public'))
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as sess:
            await sess.execute(sql_text(f'SET search_path TO "{schema}", public'))

            tenant = (await TenantRepository(sess).create(name="A")).id
            foreign_tenant = (await TenantRepository(sess).create(name="F")).id
            owner = await UserRepository(sess, tenant).create(
                email="owner@x.test", password_hash="h", roles=[Role.MEMBER]
            )
            grantee = await UserRepository(sess, tenant).create(
                email="grantee@x.test", password_hash="h", roles=[Role.MEMBER]
            )
            outsider = await UserRepository(sess, foreign_tenant).create(
                email="outsider@y.test", password_hash="h", roles=[Role.MEMBER]
            )

            indexed: list[IndexedChunk] = []

            async def _doc(filename: str) -> tuple[uuid.UUID, uuid.UUID]:
                """Owner's doc in its own collection + one matching chunk; index it."""
                coll = await CollectionRepository(sess, tenant).create(
                    owner_id=owner.id, name=filename
                )
                doc = await DocumentRepository(sess, tenant).create(
                    owner_id=owner.id,
                    collection_id=coll.id,
                    filename=filename,
                    mime_type="text/plain",
                    size_bytes=1,
                    storage_key="k",
                    status=DocumentStatus.READY,
                )
                chunks = await ChunkRepository(sess, tenant).replace_for_document(
                    doc.id,
                    [
                        ChunkInput(
                            text=chunk_body,
                            char_start=0,
                            char_end=len(chunk_body),
                            embedding=_unit_vector(_EMBED_DIM, hot),
                        )
                    ],
                )
                indexed.extend(
                    IndexedChunk(
                        chunk_id=c.id,
                        tenant_id=c.tenant_id,
                        document_id=c.document_id,
                        owner_id=owner.id,
                        collection_id=coll.id,
                        ord=c.ord,
                        text=c.text,
                        embedding=c.embedding,
                        char_start=c.char_start,
                        char_end=c.char_end,
                    )
                    for c in chunks
                )
                return coll.id, doc.id

            # All three filenames share the "quarterly" token so the relational
            # filename match can't be the differentiator — only permission is.
            _coll1, doc1 = await _doc("quarterly-alpha.txt")
            coll2, doc2 = await _doc("quarterly-beta.txt")
            _coll3, doc3 = await _doc("quarterly-gamma.txt")
            await sess.commit()

            await store.ensure_index()
            await store.upsert_chunks(indexed, refresh=True)

            # Owner A grants B: a DOCUMENT grant on doc1, a COLLECTION grant on
            # doc2's collection (cascade). doc3 stays private.
            grants = GrantsService(
                sess,
                tenant_id=tenant,
                owner_id=owner.id,
                roles=(Role.MEMBER,),
                audit=AuditSink(AuditEventRepository(sess, tenant)),
                request_id="req-live",
                source_ip="203.0.113.9",
            )
            await grants.create_grant(
                resource_type=GrantResourceType.DOCUMENT,
                resource_id=doc1,
                principal_id=grantee.id,
            )
            await grants.create_grant(
                resource_type=GrantResourceType.COLLECTION,
                resource_id=coll2,
                principal_id=grantee.id,
            )
            await sess.commit()

            gateway = _FakeGateway(_unit_vector(_EMBED_DIM, hot))
            svc = RetrievalService(sess, gateway=gateway, store=store)
            grantee_p = _principal(grantee.id, tenant)

            # search_text (OpenSearch): exactly the two granted docs, never doc3.
            passages = await svc.search_text(principal=grantee_p, query=query_text, k=10)
            assert {p.document_id for p in passages} == {doc1, doc2}

            # search_documents (relational): the same two, never doc3.
            matches = await svc.search_documents(principal=grantee_p, name_or_query="quarterly")
            assert {m.document_id for m in matches} == {doc1, doc2}

            # get_document (relational): granted -> text; non-granted -> None.
            assert await svc.get_document(principal=grantee_p, document_id=doc1) is not None
            assert await svc.get_document(principal=grantee_p, document_id=doc2) is not None
            assert await svc.get_document(principal=grantee_p, document_id=doc3) is None

            # Owner sees all three across the same three tools.
            owner_p = _principal(owner.id, tenant)
            owner_passages = await svc.search_text(principal=owner_p, query=query_text, k=10)
            assert {p.document_id for p in owner_passages} == {doc1, doc2, doc3}
            owner_matches = await svc.search_documents(principal=owner_p, name_or_query="quarterly")
            assert {m.document_id for m in owner_matches} == {doc1, doc2, doc3}
            assert await svc.get_document(principal=owner_p, document_id=doc3) is not None

            # A foreign-tenant user sees nothing from any tool (INV-1).
            outsider_p = _principal(outsider.id, foreign_tenant)
            assert await svc.search_text(principal=outsider_p, query=query_text, k=10) == []
            assert await svc.search_documents(principal=outsider_p, name_or_query="quarterly") == []
            assert await svc.get_document(principal=outsider_p, document_id=doc1) is None
    finally:
        import httpx as _httpx

        async with _httpx.AsyncClient(base_url=_OS_URL, timeout=15.0) as cleanup:
            await cleanup.delete(f"/{store._index}")  # noqa: SLF001 — test teardown
            await cleanup.delete(f"/_search/pipeline/{store._index}-hybrid")  # noqa: SLF001
        await store.aclose()
        async with engine.begin() as conn:
            await conn.execute(sql_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
