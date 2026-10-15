"""Final evidence reads must observe group membership revocations immediately."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    ChunkInput,
    ChunkRepository,
    CollectionRepository,
    DocumentRepository,
    GrantRepository,
    GroupRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import (
    DocumentStatus,
    GrantPrincipalType,
    GrantResourceType,
    GrantRole,
    Role,
)
from app.retrieval import RetrievalService

import app.db.models  # noqa: F401  isort: skip


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as current:
            yield current
    finally:
        await engine.dispose()


async def test_read_passages_rechecks_group_membership_after_revocation(
    session: AsyncSession,
) -> None:
    """One service instance cannot reuse group access after membership is removed."""
    tenant = await TenantRepository(session).create(name="Group revocation tenant")
    users = UserRepository(session, tenant.id)
    owner = await users.create(
        email=f"owner-{uuid.uuid4()}@example.test", password_hash="x", roles=[Role.MEMBER]
    )
    grantee = await users.create(
        email=f"member-{uuid.uuid4()}@example.test", password_hash="x", roles=[Role.MEMBER]
    )
    collections = CollectionRepository(session, tenant.id)
    collection = await collections.create(owner_id=owner.id, name="Shared")
    document = await DocumentRepository(session, tenant.id).create(
        owner_id=owner.id,
        collection_id=collection.id,
        filename="shared.txt",
        mime_type="text/plain",
        size_bytes=20,
        storage_key="tenant/shared.txt",
        acl_enforced=False,
        status=DocumentStatus.READY,
    )
    chunks = await ChunkRepository(session, tenant.id).replace_for_document(
        document.id, [ChunkInput(text="permissioned evidence", char_start=0, char_end=21)]
    )
    group_repository = GroupRepository(session, tenant.id)
    group = await group_repository.create(name="Reviewers", created_by=owner.id)
    await group_repository.add_member(group_id=group.id, user_id=grantee.id, added_by=owner.id)
    await GrantRepository(session, tenant.id).create(
        resource_type=GrantResourceType.COLLECTION,
        resource_id=collection.id,
        principal_type=GrantPrincipalType.GROUP,
        principal_id=group.id,
        role=GrantRole.VIEWER,
        granted_by=owner.id,
    )

    principal = Principal(user_id=grantee.id, tenant_id=tenant.id, roles=(Role.MEMBER,))
    # This API path does not call the model gateway; keep the constructor's
    # collaborator explicitly absent so the test proves only the SQL permission check.
    retrieval = RetrievalService(session, gateway=cast(Any, None))

    first = await retrieval.read_passages(principal=principal, chunk_ids=[chunks[0].id])
    assert len(first) == 1
    assert first[0].text == "permissioned evidence"

    assert await group_repository.remove_member(group_id=group.id, user_id=grantee.id)
    second = await retrieval.read_passages(principal=principal, chunk_ids=[chunks[0].id])
    assert second == []
