"""Tenant/owner persistence tests for durable conversation evidence handles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.errors import NotFoundError, ValidationError
from app.db.base import Base
from app.db.evidence_handles import HandleRepository
from app.db.repositories import ChatSessionRepository, TenantRepository, UserRepository
from app.domain.entities import Role

import app.db.models  # noqa: F401  isort: skip


class _HandleWorld:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        principal: Principal,
        own_session_id: UUID,
        other_owner_session_id: UUID,
        other_tenant_principal: Principal,
        other_tenant_session_id: UUID,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.principal = principal
        self.own_session_id = own_session_id
        self.other_owner_session_id = other_owner_session_id
        self.other_tenant_principal = other_tenant_principal
        self.other_tenant_session_id = other_tenant_session_id


@pytest_asyncio.fixture
async def handle_world() -> AsyncIterator[_HandleWorld]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            tenant_a = await TenantRepository(session).create(name="Acme")
            tenant_b = await TenantRepository(session).create(name="Globex")
            alice = await UserRepository(session, tenant_a.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(session, tenant_a.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            carol = await UserRepository(session, tenant_b.id).create(
                email="carol@globex.test", password_hash="x", roles=[Role.MEMBER]
            )
            chats_a = ChatSessionRepository(session, tenant_a.id)
            own = await chats_a.create(owner_id=alice.id, model="test/model")
            other_owner = await chats_a.create(owner_id=bob.id, model="test/model")
            other_tenant = await ChatSessionRepository(session, tenant_b.id).create(
                owner_id=carol.id, model="test/model"
            )
            await session.commit()
            yield _HandleWorld(
                sessionmaker=factory,
                principal=Principal(user_id=alice.id, tenant_id=tenant_a.id, roles=(Role.MEMBER,)),
                own_session_id=own.id,
                other_owner_session_id=other_owner.id,
                other_tenant_principal=Principal(
                    user_id=carol.id, tenant_id=tenant_b.id, roles=(Role.MEMBER,)
                ),
                other_tenant_session_id=other_tenant.id,
            )
    finally:
        await engine.dispose()


async def test_reservations_start_at_one_and_advance_in_disjoint_ranges(
    handle_world: _HandleWorld,
) -> None:
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        assert await repository.reserve() == 1
        await session.commit()

    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        assert await repository.reserve() == 1001
        await session.commit()


async def test_committed_reservation_is_not_reused_after_answer_transaction_rolls_back(
    handle_world: _HandleWorld,
) -> None:
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        first = await repository.reserve()
        await session.commit()
        assert first == 1

    # The answer transaction may fail after allocating labels. Its write rolls
    # back, but the already committed reservation remains consumed.
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        await repository.append({"S1": {"kind": "document", "document_id": "reserved"}})
        await session.rollback()

    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        assert await repository.reserve() == 1001
        await session.commit()
        assert await repository.load() == {}


async def test_append_load_is_idempotent_and_persists_ids_without_source_text(
    handle_world: _HandleWorld,
) -> None:
    entry = {
        "kind": "passage",
        "document_id": "00000000-0000-0000-0000-000000000010",
        "chunk_id": "00000000-0000-0000-0000-000000000011",
        "char_start": 10,
        "char_end": 34,
        "fingerprint": "abc123",
    }
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        await repository.append({"S1": entry})
        await repository.append({"S1": entry})
        await session.commit()

    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        assert await repository.load() == {"S1": entry}
        assert "corpus passage secret" not in str(await repository.load())


async def test_persisted_handle_cannot_be_reassigned(
    handle_world: _HandleWorld,
) -> None:
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        await repository.append({"S1": {"kind": "passage", "chunk_id": "one"}})
        await session.commit()

    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        with pytest.raises(ValidationError):
            await repository.append({"S1": {"kind": "passage", "chunk_id": "different"}})


@pytest.mark.parametrize("operation", ["reserve", "load", "append"])
async def test_same_tenant_wrong_owner_is_not_found(
    handle_world: _HandleWorld, operation: str
) -> None:
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(
            session, handle_world.principal, handle_world.other_owner_session_id
        )
        with pytest.raises(NotFoundError):
            if operation == "reserve":
                await repository.reserve()
            elif operation == "load":
                await repository.load()
            else:
                await repository.append({"S1": {"kind": "document", "document_id": "x"}})


async def test_cross_tenant_session_is_not_found(
    handle_world: _HandleWorld,
) -> None:
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(
            session, handle_world.principal, handle_world.other_tenant_session_id
        )
        with pytest.raises(NotFoundError):
            await repository.load()


async def test_reservation_capacity_must_be_between_one_and_one_thousand(
    handle_world: _HandleWorld,
) -> None:
    async with handle_world.sessionmaker() as session:
        repository = HandleRepository(session, handle_world.principal, handle_world.own_session_id)
        with pytest.raises(ValidationError):
            await repository.reserve(capacity=1001)
