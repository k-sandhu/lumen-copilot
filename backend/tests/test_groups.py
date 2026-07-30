"""Group access model — group principals widen INV-2 without leaking (ADR-0022).

The negative tests carry the weight here (ADR-0022 *Consequences*): a group grant
admits **members only**, membership is re-read per request so removal revokes
immediately, a group from another tenant is invisible, and the derived "All
members" group means tenant-wide never needs a materialized row.

Offline-safe: the same in-memory SQLite + fake-gateway harness ``test_grants.py``
uses, so the whole suite runs with no Postgres and no OpenSearch. The engine
mirror is asserted structurally (``SearchAllowFilter``) rather than against a
live index — the live engine path stays in the ``@_live`` grant tests.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    GroupRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import (
    DocumentStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    GroupKind,
    Role,
)
from app.domain.llm import Embedding
from app.retrieval import RetrievalService
from app.search import SearchAllowFilter
from app.services.audit import AuditSink
from app.services.grants_service import GrantsService
from app.services.groups_service import GroupsService

# Importing models registers them on Base.metadata.
import app.db.models  # noqa: F401  isort: skip

pytestmark = pytest.mark.asyncio


class _FakeGateway:
    """Deterministic embedder — retrieval here is exercised relationally."""

    async def embed(self, texts: list[str], **_: object) -> list[Embedding]:
        return [Embedding(vector=[0.1] * 1024) for _ in texts]


def _principal(user_id: uuid.UUID, tenant_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(Role.MEMBER,))


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
) -> tuple[uuid.UUID, uuid.UUID]:
    coll = await CollectionRepository(session, tenant_id).create(owner_id=owner_id, name="C")
    doc = await DocumentRepository(session, tenant_id).create(
        owner_id=owner_id,
        collection_id=coll.id,
        filename=filename,
        mime_type="text/plain",
        size_bytes=10,
        storage_key="k",
        acl_enforced=False,
        status=DocumentStatus.READY,
    )
    await ChunkRepository(session, tenant_id).replace_for_document(
        doc.id, [ChunkInput(text="body", char_start=0, char_end=4)]
    )
    return coll.id, doc.id


def _grants_service(
    session: AsyncSession, *, tenant_id: uuid.UUID, owner_id: uuid.UUID, roles: tuple[Role, ...]
) -> GrantsService:
    return GrantsService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        audit=AuditSink(AuditEventRepository(session, tenant_id)),
        request_id="req-test",
        source_ip="203.0.113.1",
    )


# --- membership resolution ------------------------------------------------------


async def test_membership_is_tenant_scoped_and_fails_closed(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A user with no groups resolves to an empty set — absence is denial."""
    tenant_a, _ = two_tenants
    user = await _make_user(session, tenant_a, "a@x.test")
    groups = GroupRepository(session, tenant_a)
    assert await groups.group_ids_for_user(user) == frozenset()


async def test_system_group_membership_is_derived_not_stored(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """ADR-0022 §3: every user carries the system group with **no** member rows."""
    tenant_a, _ = two_tenants
    user = await _make_user(session, tenant_a, "a@x.test")
    groups = GroupRepository(session, tenant_a)
    system, created = await groups.ensure_system_group()
    assert created is True, "first call inserts"

    assert system.kind is GroupKind.SYSTEM
    assert system.is_system
    # Nobody was explicitly added, yet the user carries it.
    assert await groups.list_member_ids(system.id) == []
    assert await groups.group_ids_for_user(user) == frozenset({system.id})


async def test_ensure_system_group_is_idempotent(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    groups = GroupRepository(session, tenant_a)
    first, first_created = await groups.ensure_system_group()
    second, second_created = await groups.ensure_system_group()
    assert first_created is True and second_created is False, (
        "only the inserting call reports creation, so only it audits"
    )
    assert first.id == second.id
    assert len([g for g in await groups.list_all() if g.is_system]) == 1


async def test_group_of_another_tenant_is_invisible(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1: a tenant-B group is not readable — and never enters A's allow-set."""
    tenant_a, tenant_b = two_tenants
    group_b = await GroupRepository(session, tenant_b).create(name="B team", created_by=None)
    assert await GroupRepository(session, tenant_a).get(group_b.id) is None


async def test_group_names_are_unique_per_tenant_case_insensitively(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    await GroupRepository(session, tenant_a).create(name="Tax Team", created_by=None)
    assert await GroupRepository(session, tenant_a).get_by_name("tax team") is not None
    # The same name in another tenant is a different group entirely.
    other = await GroupRepository(session, tenant_b).create(name="Tax Team", created_by=None)
    assert other is not None


# --- the INV-2 widening: group grants admit members only -------------------------


async def test_group_grant_admits_members_and_excludes_non_members(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The headline guarantee (ADR-0022 §5), positive and negative in one test."""
    tenant_a, _ = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    member = await _make_user(session, tenant_a, "member@x.test")
    outsider = await _make_user(session, tenant_a, "outsider@x.test")
    coll, doc = await _make_document(
        session, tenant_id=tenant_a, owner_id=owner, filename="shared.txt"
    )

    groups = GroupRepository(session, tenant_a)
    group = await groups.create(name="Tax Team", created_by=owner)
    await groups.add_member(group_id=group.id, user_id=member, added_by=owner)

    await _grants_service(
        session, tenant_id=tenant_a, owner_id=owner, roles=(Role.MEMBER,)
    ).create_grant(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=coll,
        principal_id=group.id,
        principal_type=GrantPrincipalType.GROUP,
    )

    svc = RetrievalService(session, gateway=_FakeGateway())
    seen_by_member = await svc.list_documents(principal=_principal(member, tenant_a))
    assert doc in {m.document_id for m in seen_by_member}

    # The non-member is in the same tenant and the grant exists — but not for them.
    seen_by_outsider = await svc.list_documents(principal=_principal(outsider, tenant_a))
    assert doc not in {m.document_id for m in seen_by_outsider}


async def test_removing_a_member_revokes_on_the_next_request(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """ADR-0022 §7: membership is re-read per request, so removal is immediate."""
    tenant_a, _ = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    member = await _make_user(session, tenant_a, "member@x.test")
    coll, doc = await _make_document(
        session, tenant_id=tenant_a, owner_id=owner, filename="shared.txt"
    )
    groups = GroupRepository(session, tenant_a)
    group = await groups.create(name="Tax Team", created_by=owner)
    await groups.add_member(group_id=group.id, user_id=member, added_by=owner)
    await _grants_service(
        session, tenant_id=tenant_a, owner_id=owner, roles=(Role.MEMBER,)
    ).create_grant(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=coll,
        principal_id=group.id,
        principal_type=GrantPrincipalType.GROUP,
    )

    before = await RetrievalService(session, gateway=_FakeGateway()).list_documents(
        principal=_principal(member, tenant_a)
    )
    assert doc in {m.document_id for m in before}

    assert await groups.remove_member(group_id=group.id, user_id=member) is True

    # A NEW service instance models the next request (the per-request memo is gone).
    after = await RetrievalService(session, gateway=_FakeGateway()).list_documents(
        principal=_principal(member, tenant_a)
    )
    assert doc not in {m.document_id for m in after}


async def test_deleting_a_group_cascades_membership(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """A deleted group leaves no membership row that could widen an allow-set."""
    tenant_a, _ = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    member = await _make_user(session, tenant_a, "member@x.test")
    groups = GroupRepository(session, tenant_a)
    group = await groups.create(name="Temp", created_by=owner)
    await groups.add_member(group_id=group.id, user_id=member, added_by=owner)
    assert await groups.group_ids_for_user(member) == frozenset({group.id})

    assert await groups.delete(group.id) is True
    assert await groups.group_ids_for_user(member) == frozenset()


async def test_cross_tenant_group_grant_never_widens_the_filter(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1: a grant row minted in tenant B cannot admit a tenant-A document.

    Written straight through the repository (the service would 404 first), so the
    *filter itself* is proven tenant-scoped rather than only its caller.
    """
    tenant_a, tenant_b = two_tenants
    owner_a = await _make_user(session, tenant_a, "owner@a.test")
    user_a = await _make_user(session, tenant_a, "member@a.test")
    coll_a, doc_a = await _make_document(
        session, tenant_id=tenant_a, owner_id=owner_a, filename="a.txt"
    )
    group_b = await GroupRepository(session, tenant_b).create(name="B team", created_by=None)
    await GrantRepository(session, tenant_b).create(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=coll_a,
        principal_type=GrantPrincipalType.GROUP,
        principal_id=group_b.id,
        role=GrantRole.VIEWER,
        granted_by=None,
    )

    seen = await RetrievalService(session, gateway=_FakeGateway()).list_documents(
        principal=_principal(user_a, tenant_a)
    )
    assert doc_a not in {m.document_id for m in seen}


# --- grantee validation ---------------------------------------------------------


async def test_group_grant_to_foreign_tenant_group_is_404(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Existence non-disclosure: a tenant-B group is 404, never 403 (INV-1)."""
    tenant_a, tenant_b = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    coll, _doc = await _make_document(session, tenant_id=tenant_a, owner_id=owner, filename="a.txt")
    group_b = await GroupRepository(session, tenant_b).create(name="B team", created_by=None)

    with pytest.raises(NotFoundError):
        await _grants_service(
            session, tenant_id=tenant_a, owner_id=owner, roles=(Role.MEMBER,)
        ).create_grant(
            resource_type=GrantResourceType.COLLECTION,
            resource_id=coll,
            principal_id=group_b.id,
            principal_type=GrantPrincipalType.GROUP,
        )


async def test_role_principal_is_still_rejected(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """ADR-0022 §4: ROLE stays unsupported — INV-2 is not widened for free."""
    tenant_a, _ = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    coll, _doc = await _make_document(session, tenant_id=tenant_a, owner_id=owner, filename="a.txt")
    with pytest.raises(ValidationError, match="unsupported_principal_type|group grants"):
        await _grants_service(
            session, tenant_id=tenant_a, owner_id=owner, roles=(Role.MEMBER,)
        ).create_grant(
            resource_type=GrantResourceType.COLLECTION,
            resource_id=coll,
            principal_id=uuid.uuid4(),
            principal_type=GrantPrincipalType.ROLE,
        )


# --- the two homes of the rule agree --------------------------------------------


class _RecordingStore:
    """A stand-in OpenSearch store that captures the allow-filter it is handed.

    The point is to observe what ``RetrievalService`` actually sends to the
    engine. Returning no hits is fine — the assertion is about the filter, and
    the service short-circuits on an empty result without touching Postgres.
    """

    def __init__(self) -> None:
        self.allow: SearchAllowFilter | None = None

    async def ensure_index(self) -> None:
        return None

    async def hybrid_search(self, *, allow: SearchAllowFilter, **_: object) -> list[object]:
        self.allow = allow
        return []


async def test_group_grant_reaches_the_engine_filter_not_just_sql(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The SQL predicate and the ENGINE mirror must admit the same collection.

    ``_document_permitted`` and ``SearchAllowFilter`` are the only two places the
    rule lives (ADR-0019 §2, widened by ADR-0022 §5), and they are easy to let
    drift because only one of them is exercised by a relational test. This drives
    the real ``RetrievalService.search`` path with a recording store and asserts
    the group-granted collection reaches the **engine filter** — so dropping
    ``group_ids=allow_set.group_ids`` from ``_allow_filter`` fails here, which a
    ``list_documents``-only assertion would not catch.
    """
    tenant_a, _ = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    member = await _make_user(session, tenant_a, "member@x.test")
    coll, doc = await _make_document(
        session, tenant_id=tenant_a, owner_id=owner, filename="shared.txt"
    )
    groups = GroupRepository(session, tenant_a)
    group = await groups.create(name="Tax Team", created_by=owner)
    await groups.add_member(group_id=group.id, user_id=member, added_by=owner)
    await _grants_service(
        session, tenant_id=tenant_a, owner_id=owner, roles=(Role.MEMBER,)
    ).create_grant(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=coll,
        principal_id=group.id,
        principal_type=GrantPrincipalType.GROUP,
    )

    store = _RecordingStore()
    svc = RetrievalService(session, gateway=_FakeGateway(), store=store)  # type: ignore[arg-type]
    await svc.search(principal=_principal(member, tenant_a), query="anything", k=5)

    assert store.allow is not None, "the engine must have been queried"
    # The engine leg: the group-granted collection is in the resolved id-set.
    assert coll in store.allow.granted_collection_ids
    # ...and the SQL leg admits the same document.
    seen = await RetrievalService(session, gateway=_FakeGateway()).list_documents(
        principal=_principal(member, tenant_a)
    )
    assert doc in {m.document_id for m in seen}


async def test_engine_filter_excludes_a_non_member(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The negative half: a non-member's engine filter carries no group grant."""
    tenant_a, _ = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    outsider = await _make_user(session, tenant_a, "outsider@x.test")
    coll, _doc = await _make_document(
        session, tenant_id=tenant_a, owner_id=owner, filename="shared.txt"
    )
    group = await GroupRepository(session, tenant_a).create(name="Tax Team", created_by=owner)
    await _grants_service(
        session, tenant_id=tenant_a, owner_id=owner, roles=(Role.MEMBER,)
    ).create_grant(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=coll,
        principal_id=group.id,
        principal_type=GrantPrincipalType.GROUP,
    )

    store = _RecordingStore()
    svc = RetrievalService(session, gateway=_FakeGateway(), store=store)  # type: ignore[arg-type]
    await svc.search(principal=_principal(outsider, tenant_a), query="anything", k=5)

    assert store.allow is not None
    assert coll not in store.allow.granted_collection_ids


async def test_empty_group_set_degrades_to_user_only_grants(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The fail-closed default: no group ids ⇒ no group leg is rendered at all."""
    tenant_a, _ = two_tenants
    owner = await _make_user(session, tenant_a, "owner@x.test")
    outsider = await _make_user(session, tenant_a, "outsider@x.test")
    coll, _doc = await _make_document(session, tenant_id=tenant_a, owner_id=owner, filename="a.txt")
    group = await GroupRepository(session, tenant_a).create(name="Tax Team", created_by=owner)
    await _grants_service(
        session, tenant_id=tenant_a, owner_id=owner, roles=(Role.MEMBER,)
    ).create_grant(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=coll,
        principal_id=group.id,
        principal_type=GrantPrincipalType.GROUP,
    )

    _docs, colls = await GrantRepository(session, tenant_a).granted_resource_ids(outsider)
    assert colls == frozenset()


# --- GroupsService: admin gate, immutability, audit (ADR-0022 §2) ----------------


def _groups_service(
    session: AsyncSession, *, tenant_id: uuid.UUID, actor_id: uuid.UUID, roles: tuple[Role, ...]
) -> GroupsService:
    return GroupsService(
        session,
        tenant_id=tenant_id,
        actor_id=actor_id,
        roles=roles,
        audit=AuditSink(AuditEventRepository(session, tenant_id)),
        request_id="req-test",
        source_ip="203.0.113.1",
    )


async def test_member_may_not_manage_groups(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-5: group management is admin-only — 403, not a silent no-op."""
    tenant_a, _ = two_tenants
    member = await _make_user(session, tenant_a, "member@x.test")
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=member, roles=(Role.MEMBER,))
    with pytest.raises(ForbiddenError):
        await svc.create_group(name="Tax Team")
    with pytest.raises(ForbiddenError):
        await svc.list_groups()


async def test_admin_creates_renames_and_deletes_a_group(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    admin = await _make_user(session, tenant_a, "admin@x.test")
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=admin, roles=(Role.ADMIN,))

    group = await svc.create_group(name="  Tax Team  ")
    assert group.name == "Tax Team"  # trimmed
    renamed = await svc.rename_group(group.id, name="Tax & Audit")
    assert renamed.name == "Tax & Audit"
    await svc.delete_group(group.id)
    with pytest.raises(NotFoundError):
        await svc.get_group(group.id)


async def test_duplicate_group_name_is_conflict_case_insensitively(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    admin = await _make_user(session, tenant_a, "admin@x.test")
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=admin, roles=(Role.ADMIN,))
    await svc.create_group(name="Tax Team")
    with pytest.raises(ConflictError, match="already exists"):
        await svc.create_group(name="tax team")


async def test_blank_group_name_is_rejected(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    admin = await _make_user(session, tenant_a, "admin@x.test")
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=admin, roles=(Role.ADMIN,))
    with pytest.raises(ValidationError):
        await svc.create_group(name="   ")


async def test_system_group_cannot_be_renamed_deleted_or_populated(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """ADR-0022 §3: the derived group is listed but immutable."""
    tenant_a, _ = two_tenants
    admin = await _make_user(session, tenant_a, "admin@x.test")
    system, _ = await GroupRepository(session, tenant_a).ensure_system_group()
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=admin, roles=(Role.ADMIN,))

    assert system.id in {g.id for g in await svc.list_groups()}
    for call in (
        svc.rename_group(system.id, name="Everyone"),
        svc.delete_group(system.id),
        svc.add_member(system.id, user_id=admin),
        svc.remove_member(system.id, user_id=admin),
    ):
        with pytest.raises(ConflictError, match="system_group_immutable|managed automatically"):
            await call


async def test_foreign_tenant_group_is_404_for_admin(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-1: a tenant-B group is 404 for a tenant-A admin, never 403."""
    tenant_a, tenant_b = two_tenants
    admin = await _make_user(session, tenant_a, "admin@x.test")
    group_b = await GroupRepository(session, tenant_b).create(name="B team", created_by=None)
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=admin, roles=(Role.ADMIN,))
    with pytest.raises(NotFoundError):
        await svc.get_group(group_b.id)


async def test_adding_a_foreign_tenant_user_is_404(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, tenant_b = two_tenants
    admin = await _make_user(session, tenant_a, "admin@x.test")
    outsider = await _make_user(session, tenant_b, "outsider@b.test")
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=admin, roles=(Role.ADMIN,))
    group = await svc.create_group(name="Tax Team")
    with pytest.raises(NotFoundError):
        await svc.add_member(group.id, user_id=outsider)
    assert await svc.list_member_ids(group.id) == []


async def test_membership_changes_are_idempotent_and_audited(
    session: AsyncSession, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """INV-6: every mutation is provable after the fact; repeats are no-ops."""
    tenant_a, _ = two_tenants
    admin = await _make_user(session, tenant_a, "admin@x.test")
    member = await _make_user(session, tenant_a, "member@x.test")
    svc = _groups_service(session, tenant_id=tenant_a, actor_id=admin, roles=(Role.ADMIN,))
    group = await svc.create_group(name="Tax Team")

    await svc.add_member(group.id, user_id=member)
    await svc.add_member(group.id, user_id=member)  # idempotent
    assert await svc.list_member_ids(group.id) == [member]
    await svc.remove_member(group.id, user_id=member)
    await svc.remove_member(group.id, user_id=member)  # idempotent
    assert await svc.list_member_ids(group.id) == []

    actions = {
        e.action for e in await AuditEventRepository(session, tenant_a).list_recent(limit=20)
    }
    assert AuditAction.GROUP_CREATED.value in actions
    assert AuditAction.GROUP_MEMBER_ADDED.value in actions
    assert AuditAction.GROUP_MEMBER_REMOVED.value in actions
