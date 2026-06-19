"""Chat service tests — sessions CRUD + send + history (CC-6 #24 / CC-11 #26).

Offline (in-memory SQLite). Exercises the orchestration directly: model
allow-list validation (unknown → 422, INV-8), ownership/tenancy (other-owner /
cross-tenant → ``None`` → 404, INV-1/INV-2), the 202 send (user message
persisted, stream_id minted), and message-history pagination with hydrated
citations.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    MessageRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import MessageRole, Role
from app.realtime.backplane import InMemoryBackplane
from app.services.chat_service import ChatService

import app.db.models  # noqa: F401  isort: skip


class _World:
    def __init__(
        self,
        *,
        tenant_a: uuid.UUID,
        tenant_b: uuid.UUID,
        alice: uuid.UUID,
        bob: uuid.UUID,
        carol: uuid.UUID,
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice
        self.bob = bob
        self.carol = carol


@pytest_asyncio.fixture
async def world_and_factory() -> AsyncIterator[tuple[_World, async_sessionmaker[AsyncSession]]]:
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
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, ta.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            carol = await UserRepository(seed, tb.id).create(
                email="carol@globex.test", password_hash="x", roles=[Role.MEMBER]
            )
            await seed.commit()
            yield (
                _World(
                    tenant_a=ta.id,
                    tenant_b=tb.id,
                    alice=alice.id,
                    bob=bob.id,
                    carol=carol.id,
                ),
                factory,
            )
    finally:
        await engine.dispose()


def _settings() -> Settings:
    return get_settings()


def _service(session: AsyncSession, *, tenant_id: uuid.UUID, owner_id: uuid.UUID) -> ChatService:
    return ChatService(session, tenant_id=tenant_id, owner_id=owner_id, settings=_settings())


# --- create / model validation ---------------------------------------------


async def test_create_session_defaults_model(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        view = await svc.create_session(title="hello", model=None)
        await session.commit()
    # The default model is the registry's is_default entry.
    default = next(m.id for m in _settings().chat_model_registry if m.is_default)
    assert view.session.model == default
    assert view.message_count == 0


async def test_create_session_unknown_model_is_422(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        with pytest.raises(ValidationError):
            await svc.create_session(title="x", model="totally/unknown-model")


# --- ownership / tenancy ----------------------------------------------------


async def test_get_other_owner_session_is_none(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        bob_session = await ChatSessionRepository(session, world.tenant_a).create(
            owner_id=world.bob, model="anthropic/claude-opus-4.8"
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        # Alice cannot see Bob's session (same tenant) → None → 404 (INV-2).
        assert await svc.get_session(bob_session.id) is None


async def test_get_cross_tenant_session_is_none(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        carol_session = await ChatSessionRepository(session, world.tenant_b).create(
            owner_id=world.carol, model="anthropic/claude-opus-4.8"
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        assert await svc.get_session(carol_session.id) is None


async def test_list_sessions_only_callers_own(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        await ChatSessionRepository(session, world.tenant_a).create(
            owner_id=world.alice, model="anthropic/claude-opus-4.8", title="mine"
        )
        await ChatSessionRepository(session, world.tenant_a).create(
            owner_id=world.bob, model="anthropic/claude-opus-4.8", title="bobs"
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        page = await svc.list_sessions(cursor=None, limit=20)
    titles = {v.session.title for v in page.items}
    assert titles == {"mine"}


async def test_update_other_owner_session_is_none(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        bob_session = await ChatSessionRepository(session, world.tenant_a).create(
            owner_id=world.bob, model="anthropic/claude-opus-4.8"
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        assert await svc.update_session(bob_session.id, title="hijack", model=None) is None


# --- send -------------------------------------------------------------------


async def test_send_persists_user_message_and_returns_stream_id(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    backplane = InMemoryBackplane()
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        await session.commit()
        result = await svc.send_message(
            created.session.id, content="hi there", model=None, backplane=backplane
        )
        await session.commit()
    assert result is not None
    assert result.user_message.role == MessageRole.USER
    assert result.user_message.content == "hi there"
    assert result.stream_id  # a non-empty stream id
    # The model resolves to the session default.
    assert result.model == created.session.model
    # The stream is bound to the asking principal (owner + tenant) so the WS
    # consumer can authorize it (INV-1/INV-2, spec 0004 §2.1/§2.2).
    owner = await backplane.get_owner(result.stream_id)
    assert owner is not None
    assert owner.owner_id == world.alice
    assert owner.tenant_id == world.tenant_a


async def test_send_unknown_model_is_422_before_persist(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        await session.commit()
        with pytest.raises(ValidationError):
            await svc.send_message(
                created.session.id,
                content="hi",
                model="nope/nope",
                backplane=InMemoryBackplane(),
            )
        # Nothing was persisted (the validation precedes the write).
        msgs = await MessageRepository(session, world.tenant_a).list_for_session(created.session.id)
    assert msgs == []


async def test_send_to_other_owner_session_is_none(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        bob_session = await ChatSessionRepository(session, world.tenant_a).create(
            owner_id=world.bob, model="anthropic/claude-opus-4.8"
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        assert (
            await svc.send_message(
                bob_session.id, content="hi", model=None, backplane=InMemoryBackplane()
            )
            is None
        )


# --- history ----------------------------------------------------------------


async def test_list_messages_pagination_is_consistent_and_complete(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        repo = MessageRepository(session, world.tenant_a)
        for i in range(5):
            await repo.add(session_id=created.session.id, role=MessageRole.USER, content=f"m{i}")
        await session.commit()

        # Source of truth for the order (the keyset must agree with this single
        # full-list ORDER BY (created_at, id) ascending — robust to tied
        # second-precision SQLite timestamps).
        full = await repo.list_for_session_page(created.session.id, limit=100)
        expected_order = [m.content for m in full]

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            page = await svc.list_messages(created.session.id, cursor=cursor, limit=2)
            assert page is not None
            assert len(page.items) <= 2
            seen.extend(v.message.content for v in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
    # No dupes, every message returned, and in the same order as the full query.
    assert seen == expected_order
    assert set(seen) == {"m0", "m1", "m2", "m3", "m4"}


async def test_list_messages_other_owner_is_none(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        bob_session = await ChatSessionRepository(session, world.tenant_a).create(
            owner_id=world.bob, model="anthropic/claude-opus-4.8"
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        assert await svc.list_messages(bob_session.id, cursor=None, limit=20) is None


async def test_invalid_cursor_is_rejected(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        with pytest.raises(ValidationError):
            await svc.list_sessions(cursor="not-a-real-cursor!!!", limit=20)
