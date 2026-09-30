"""SQLite regression coverage for permissioned document discovery and bounded reads."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.db.models  # noqa: F401  register ORM tables before create_all
from app.auth.principal import Principal
from app.db import models
from app.db.base import Base
from app.db.repositories import (
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    GroupRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    DocumentStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    Role,
    SourceStatus,
)
from app.llm import LLMGateway
from app.retrieval import RetrievalService


@dataclass(frozen=True)
class _World:
    tenant: uuid.UUID
    other_tenant: uuid.UUID
    alice: uuid.UUID
    bob: uuid.UUID
    other_user: uuid.UUID
    own_collection: uuid.UUID
    other_collection: uuid.UUID
    drive_source: uuid.UUID
    web_source: uuid.UUID
    ids: dict[str, uuid.UUID]


@pytest_asyncio.fixture
async def discovery_session() -> AsyncIterator[tuple[AsyncSession, _World]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Acme")
            other_tenant = await TenantRepository(session).create(name="Globex")
            alice = await UserRepository(session, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(session, tenant.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            other_user = await UserRepository(session, other_tenant.id).create(
                email="other@globex.test", password_hash="x", roles=[Role.MEMBER]
            )
            own_collection = await CollectionRepository(session, tenant.id).create(
                owner_id=alice.id, name="Alice files"
            )
            other_collection = await CollectionRepository(session, tenant.id).create(
                owner_id=bob.id, name="Bob files"
            )
            foreign_collection = await CollectionRepository(session, other_tenant.id).create(
                owner_id=other_user.id, name="Foreign files"
            )
            drive = await SourceRepository(session, tenant.id).create(
                owner_id=alice.id,
                type="gdrive",
                config={},
                status=SourceStatus.READY,
            )
            web = await SourceRepository(session, tenant.id).create(
                owner_id=alice.id,
                type="web",
                config={},
                status=SourceStatus.READY,
            )
            now = datetime(2026, 8, 20, 12, tzinfo=UTC)
            ids: dict[str, uuid.UUID] = {}

            async def add_doc(
                key: str,
                *,
                owner_id: uuid.UUID,
                collection_id: uuid.UUID,
                filename: str,
                title: str,
                source_path: str | None = None,
                source_id: uuid.UUID | None = None,
                mime_type: str = "text/plain",
                metadata: dict[str, object] | None = None,
                created_at: datetime | None = None,
                acl_enforced: bool = False,
                acl_principals: Sequence[str] | None = None,
                acl_synced_at: datetime | None = None,
                chunks: Sequence[tuple[str, int, int]] | None = None,
                tenant_id: uuid.UUID | None = None,
            ) -> uuid.UUID:
                effective_tenant = tenant_id or tenant.id
                doc = await DocumentRepository(session, effective_tenant).create(
                    owner_id=owner_id,
                    collection_id=collection_id,
                    filename=filename,
                    mime_type=mime_type,
                    size_bytes=128,
                    storage_key=f"{effective_tenant}/{filename}",
                    acl_enforced=acl_enforced,
                    status=DocumentStatus.READY,
                    source_id=source_id,
                    title=title,
                    source_path=source_path,
                    source_modified_at=created_at,
                    discovery_metadata=metadata or {},
                    acl_principals=acl_principals,
                    acl_synced_at=acl_synced_at,
                )
                ids[key] = doc.id
                if created_at is not None:
                    await session.execute(
                        update(models.Document)
                        .where(models.Document.id == doc.id)
                        .values(created_at=created_at)
                    )
                if chunks is not None:
                    await ChunkRepository(session, effective_tenant).replace_for_document(
                        doc.id,
                        [
                            ChunkInput(text=text, char_start=start, char_end=end)
                            for text, start, end in chunks
                        ],
                    )
                return doc.id

            await add_doc(
                "northstar",
                owner_id=alice.id,
                collection_id=own_collection.id,
                filename="upload-01.bin",
                title="Northstar Budget Workbook",
                source_path="Finance/2026/Budget.xlsx",
                source_id=drive.id,
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                metadata={"keywords": ["covenant", "redemption"], "team": "Treasury"},
                created_at=now,
            )
            await add_doc(
                "alpha",
                owner_id=alice.id,
                collection_id=own_collection.id,
                filename="alpha.txt",
                title="Alpha",
                source_path="Finance/alpha.txt",
                source_id=drive.id,
                created_at=now - timedelta(days=20),
            )
            await add_doc(
                "charlie",
                owner_id=alice.id,
                collection_id=own_collection.id,
                filename="charlie.txt",
                title="Charlie",
                source_path="Finance/charlie.txt",
                source_id=drive.id,
                created_at=now - timedelta(days=10),
            )
            await add_doc(
                "shared",
                owner_id=bob.id,
                collection_id=other_collection.id,
                filename="shared.txt",
                title="Delta Shared",
                source_path="Shared/delta.txt",
                source_id=drive.id,
                created_at=now - timedelta(days=5),
            )
            await add_doc(
                "denied",
                owner_id=bob.id,
                collection_id=other_collection.id,
                filename="bravo.txt",
                title="Bravo Private",
                source_path="Private/bravo.txt",
                source_id=drive.id,
                created_at=now - timedelta(days=15),
            )
            await add_doc(
                "revoked",
                owner_id=bob.id,
                collection_id=other_collection.id,
                filename="echo.txt",
                title="Echo Revoked ACL",
                source_path="Private/echo.txt",
                source_id=drive.id,
                acl_enforced=True,
                acl_principals=[f"user:{bob.id}"],
                acl_synced_at=now,
                created_at=now,
            )
            await add_doc(
                "foreign",
                owner_id=other_user.id,
                collection_id=foreign_collection.id,
                filename="foxtrot.txt",
                title="Foxtrot Foreign Tenant",
                source_id=None,
                tenant_id=other_tenant.id,
                created_at=now,
            )
            await add_doc(
                "same_one",
                owner_id=alice.id,
                collection_id=own_collection.id,
                filename="same-1.txt",
                title="Same Title",
                source_id=web.id,
                mime_type="text/html",
                created_at=now,
            )
            await add_doc(
                "same_two",
                owner_id=alice.id,
                collection_id=own_collection.id,
                filename="same-2.txt",
                title="Same Title",
                source_id=web.id,
                mime_type="text/html",
                created_at=now,
            )
            await add_doc(
                "range",
                owner_id=alice.id,
                collection_id=own_collection.id,
                filename="range.txt",
                title="Range source",
                source_id=web.id,
                chunks=[("0123456789", 0, 10), ("56789abcde", 5, 15), ("fghijkl", 15, 22)],
            )
            await add_doc(
                "neighbor",
                owner_id=alice.id,
                collection_id=own_collection.id,
                filename="neighbor.txt",
                title="Neighbor source",
                chunks=[("must not be mixed", 0, 17)],
            )
            await GrantRepository(session, tenant.id).create(
                resource_type=GrantResourceType.DOCUMENT,
                resource_id=ids["shared"],
                principal_type=GrantPrincipalType.USER,
                principal_id=alice.id,
                role=GrantRole.VIEWER,
                granted_by=bob.id,
            )
            await session.commit()
            yield (
                session,
                _World(
                    tenant=tenant.id,
                    other_tenant=other_tenant.id,
                    alice=alice.id,
                    bob=bob.id,
                    other_user=other_user.id,
                    own_collection=own_collection.id,
                    other_collection=other_collection.id,
                    drive_source=drive.id,
                    web_source=web.id,
                    ids=ids,
                ),
            )
    finally:
        await engine.dispose()


def _principal(user_id: uuid.UUID, tenant_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, tenant_id=tenant_id, roles=(Role.MEMBER,))


def _service(session: AsyncSession) -> RetrievalService:
    # These cases exercise relational discovery/read only; gateway is never called.
    return RetrievalService(session, gateway=cast(LLMGateway, None))


def _titles(page: object) -> list[str]:
    return [item.title for item in page.items]  # type: ignore[attr-defined]


async def test_find_documents_searches_distinct_title_path_and_metadata(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)

    by_title = await service.find_documents(principal=principal, query="Northstar Budget")
    by_path = await service.find_documents(principal=principal, query="Finance/2026/Budget")
    by_metadata = await service.find_documents(principal=principal, query="redemption")

    assert [item.document_id for item in by_title.items] == [world.ids["northstar"]]
    assert [item.document_id for item in by_path.items] == [world.ids["northstar"]]
    assert [item.document_id for item in by_metadata.items] == [world.ids["northstar"]]
    doc = by_metadata.items[0]
    assert doc.title == "Northstar Budget Workbook"
    assert doc.filename == "upload-01.bin"
    assert doc.source_path == "Finance/2026/Budget.xlsx"
    assert doc.source == "gdrive"
    assert doc.mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert doc.metadata["team"] == "Treasury"


async def test_find_documents_literal_substrings_are_not_sql_wildcards(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    page = await _service(session).find_documents(
        principal=_principal(world.alice, world.tenant), query="Budget%"
    )
    assert [item.document_id for item in page.items] == []


async def test_find_documents_paginates_only_after_permission_filtering(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)
    cursor = None
    titles: list[str] = []
    seen: list[uuid.UUID] = []
    while True:
        page = await service.find_documents(principal=principal, limit=1, cursor=cursor)
        titles.extend(_titles(page))
        seen.extend(item.document_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert titles == [
        "Alpha",
        "Charlie",
        "Delta Shared",
        "Neighbor source",
        "Northstar Budget Workbook",
        "Range source",
        "Same Title",
        "Same Title",
    ]
    assert world.ids["denied"] not in seen
    assert world.ids["revoked"] not in seen
    assert world.ids["foreign"] not in seen
    assert len(seen) == len(set(seen))


async def test_find_documents_rechecks_permissions_on_each_cursor_page(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)

    first = await service.find_documents(principal=principal, limit=1)
    assert _titles(first) == ["Alpha"]
    assert first.next_cursor is not None

    # The later page must re-evaluate the grant rather than carry forward a stale allow-set.
    await session.execute(
        delete(models.Grant).where(
            models.Grant.tenant_id == world.tenant,
            models.Grant.resource_id == world.ids["shared"],
        )
    )
    await session.commit()

    cursor = first.next_cursor
    later_ids: list[uuid.UUID] = []
    while cursor is not None:
        page = await service.find_documents(principal=principal, limit=1, cursor=cursor)
        later_ids.extend(item.document_id for item in page.items)
        cursor = page.next_cursor

    assert world.ids["shared"] not in later_ids


async def test_find_documents_cursor_is_bound_to_tenant_filters_and_sort(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)
    page = await service.find_documents(principal=principal, query="", limit=1)
    assert page.next_cursor is not None

    from app.core.errors import AppError

    with pytest.raises(AppError):
        await service.find_documents(
            principal=principal, query="different", limit=1, cursor=page.next_cursor
        )
    with pytest.raises(AppError):
        await service.find_documents(
            principal=_principal(world.bob, world.tenant), limit=1, cursor=page.next_cursor
        )
    with pytest.raises(AppError):
        await service.find_documents(
            principal=principal, limit=1, sort="title_asc", cursor="not-a-cursor"
        )


async def test_find_documents_stable_title_sort_uses_document_id_tiebreaker(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    page = await _service(session).find_documents(
        principal=_principal(world.alice, world.tenant), query="Same Title", sort="title_asc"
    )
    same = [item.document_id for item in page.items]
    assert same == sorted((world.ids["same_one"], world.ids["same_two"]), key=str)


async def test_find_documents_filters_source_mime_dates_and_scope_intersections(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)
    after = datetime(2026, 8, 1, tzinfo=UTC)
    before = datetime(2026, 8, 21, tzinfo=UTC)
    page = await service.find_documents(
        principal=principal,
        source="web",
        mime_type="text/html",
        created_after=after,
        created_before=before,
        collection_ids=[world.own_collection],
        document_ids=[world.ids["same_one"], world.ids["same_two"], world.ids["northstar"]],
    )
    ids = {item.document_id for item in page.items}
    assert ids == {world.ids["same_one"], world.ids["same_two"]}
    assert all(item.source == "web" and item.mime_type == "text/html" for item in page.items)


async def test_find_documents_filters_and_sorts_by_source_modified_time(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)
    after = datetime(2026, 8, 1, tzinfo=UTC)
    before = datetime(2026, 8, 16, tzinfo=UTC)

    bounded = await service.find_documents(
        principal=principal,
        modified_after=after,
        modified_before=before,
        document_ids=[
            world.ids["alpha"],
            world.ids["charlie"],
            world.ids["shared"],
            world.ids["range"],
        ],
    )
    assert [(item.document_id, item.source_modified_at) for item in bounded.items] == [
        (world.ids["charlie"], datetime(2026, 8, 10, 12)),
        (world.ids["shared"], datetime(2026, 8, 15, 12)),
    ]

    asc = await service.find_documents(
        principal=principal,
        sort="modified_asc",
        document_ids=[world.ids["alpha"], world.ids["charlie"], world.ids["shared"]],
    )
    desc = await service.find_documents(
        principal=principal,
        sort="modified_desc",
        document_ids=[world.ids["alpha"], world.ids["charlie"], world.ids["shared"]],
    )
    expected_asc = [world.ids["alpha"], world.ids["charlie"], world.ids["shared"]]
    expected_dates = [
        datetime(2026, 7, 31, 12),
        datetime(2026, 8, 10, 12),
        datetime(2026, 8, 15, 12),
    ]
    assert [item.document_id for item in asc.items] == expected_asc
    assert [item.source_modified_at for item in asc.items] == expected_dates
    assert [item.document_id for item in desc.items] == list(reversed(expected_asc))
    assert [item.source_modified_at for item in desc.items] == list(reversed(expected_dates))


async def test_find_documents_rejects_reversed_source_modified_range(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    from app.core.errors import ValidationError

    session, world = discovery_session
    with pytest.raises(ValidationError):
        await _service(session).find_documents(
            principal=_principal(world.alice, world.tenant),
            modified_after=datetime(2026, 8, 21, tzinfo=UTC),
            modified_before=datetime(2026, 8, 1, tzinfo=UTC),
        )


async def test_read_document_returns_whole_overlapping_chunks_without_fabricated_continuation(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)
    start = 8
    pages = []
    for _ in range(4):
        page = await service.read_document(
            principal=principal,
            document_id=world.ids["range"],
            start=start,
            end=18,
            max_passages=1,
        )
        assert page is not None
        pages.append(page)
        if page.next_start is None:
            break
        start = page.next_start

    assert len(pages) == 3
    assert [p.text for page in pages for p in page.passages] == [
        "0123456789",
        "56789abcde",
        "fghijkl",
    ]
    assert [(page.returned_start, page.returned_end) for page in pages] == [
        (0, 10),
        (5, 15),
        (15, 22),
    ]
    assert [page.next_start for page in pages] == [10, 15, None]
    assert all(page.total_length == 22 for page in pages)
    assert all(len(page.passages) <= 1 for page in pages)
    assert all(p.document_id == world.ids["range"] for page in pages for p in page.passages)


async def test_read_document_enforces_permission_and_scope_intersection(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    service = _service(session)
    principal = _principal(world.alice, world.tenant)

    # A direct grant admits the shared document; revoked mirror and foreign owner do not.
    assert (
        await service.read_document(principal=principal, document_id=world.ids["shared"])
        is not None
    )
    assert (
        await service.read_document(principal=principal, document_id=world.ids["revoked"]) is None
    )
    assert await service.read_document(principal=principal, document_id=world.ids["denied"]) is None
    assert (
        await service.read_document(
            principal=principal,
            document_id=world.ids["range"],
            collection_ids=[world.other_collection],
        )
        is None
    )


async def test_discovery_and_range_reads_refresh_revoked_group_membership(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    session, world = discovery_session
    groups = GroupRepository(session, world.tenant)
    group = await groups.create(name="Readers", created_by=world.bob)
    await groups.add_member(group_id=group.id, user_id=world.alice, added_by=world.bob)
    await GrantRepository(session, world.tenant).create(
        resource_type=GrantResourceType.DOCUMENT,
        resource_id=world.ids["denied"],
        principal_type=GrantPrincipalType.GROUP,
        principal_id=group.id,
        role=GrantRole.VIEWER,
        granted_by=world.bob,
    )
    service = _service(session)
    principal = _principal(world.alice, world.tenant)
    assert (await service.find_documents(principal=principal, query="Bravo")).items
    assert await service.read_document(principal=principal, document_id=world.ids["denied"])
    assert await service.get_document(principal=principal, document_id=world.ids["denied"])
    assert await groups.remove_member(group_id=group.id, user_id=world.alice)
    # Legacy D-handle reads must also refresh before any canonical read does so.
    assert await service.get_document(principal=principal, document_id=world.ids["denied"]) is None
    assert not (await service.find_documents(principal=principal, query="Bravo")).items
    assert await service.read_document(principal=principal, document_id=world.ids["denied"]) is None
    assert await service.get_document(principal=principal, document_id=world.ids["denied"]) is None


async def test_equal_end_chunks_require_a_larger_budget_instead_of_skipping_evidence(
    discovery_session: tuple[AsyncSession, _World],
) -> None:
    from app.core.errors import ValidationError

    session, world = discovery_session
    await ChunkRepository(session, world.tenant).replace_for_document(
        world.ids["range"],
        [
            ChunkInput(text="first", char_start=0, char_end=10),
            ChunkInput(text="second", char_start=5, char_end=10),
        ],
    )
    service = _service(session)
    principal = _principal(world.alice, world.tenant)
    with pytest.raises(ValidationError, match="max_passages"):
        await service.read_document(
            principal=principal,
            document_id=world.ids["range"],
            max_passages=1,
        )
    page = await service.read_document(
        principal=principal,
        document_id=world.ids["range"],
        max_passages=2,
    )
    assert page is not None
    assert [passage.text for passage in page.passages] == ["first", "second"]
    assert page.next_start is None
    assert (
        await service.read_document(
            principal=principal,
            document_id=world.ids["range"],
            document_ids=[world.ids["shared"]],
        )
        is None
    )
