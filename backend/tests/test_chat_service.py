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
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.db import models as db_models
from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    LlmProviderRepository,
    MessageRepository,
    TenantRepository,
    ToolInvocationRepository,
    UserPreferenceRepository,
    UserRepository,
)
from app.domain.entities import LlmProviderStatus, MessageRole, Role
from app.realtime.backplane import InMemoryBackplane
from app.services.chat_service import ChatService
from app.services.provider_models import make_provider_model_id

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


# --- per-tenant provider model allow-list (PR 2a) ---------------------------


async def _seed_provider(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    owner_id: uuid.UUID,
    models: list[dict[str, object]],
    enabled: bool = True,
) -> uuid.UUID:
    """Seed one READY llm_providers row with a discovered-model snapshot."""
    repo = LlmProviderRepository(session, tenant_id)
    provider = await repo.create(
        owner_id=owner_id,
        name="Prov",
        provider_type="openai_compatible",
        base_url="https://prov.example.com/v1",
        api_key_secret_ref=None,
        secret_hint=None,
    )
    await repo.set_discovery(
        provider.id,
        status=LlmProviderStatus.READY,
        discovered_models=models,
        last_error=None,
        last_discovery_at=None,
    )
    if not enabled:
        await repo.update(provider.id, enabled=False)
    return provider.id


async def test_create_session_accepts_valid_provider_model(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        pid = await _seed_provider(
            session,
            tenant_id=world.tenant_a,
            owner_id=world.alice,
            models=[{"id": "openai/gpt-4o", "label": "GPT-4o"}],
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        model_id = make_provider_model_id(pid, "openai/gpt-4o")
        view = await svc.create_session(title="x", model=model_id)
        await session.commit()
    # The namespaced id is a valid model: the session persists it verbatim.
    assert view.session.model == model_id


async def test_create_session_disabled_provider_model_is_422(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        pid = await _seed_provider(
            session,
            tenant_id=world.tenant_a,
            owner_id=world.alice,
            models=[{"id": "openai/gpt-4o", "label": "GPT-4o"}],
            enabled=False,
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        with pytest.raises(ValidationError):
            await svc.create_session(title="x", model=make_provider_model_id(pid, "openai/gpt-4o"))


async def test_create_session_unknown_raw_model_is_422(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        pid = await _seed_provider(
            session,
            tenant_id=world.tenant_a,
            owner_id=world.alice,
            models=[{"id": "openai/gpt-4o", "label": "GPT-4o"}],
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        # The provider is enabled but does not list this raw model → 422.
        unknown = make_provider_model_id(pid, "openai/not-real")
        with pytest.raises(ValidationError):
            await svc.create_session(title="x", model=unknown)


async def test_create_session_cross_tenant_provider_model_is_422(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        # A provider registered in tenant B, referenced by a tenant-A caller.
        pid = await _seed_provider(
            session,
            tenant_id=world.tenant_b,
            owner_id=world.carol,
            models=[{"id": "openai/gpt-4o", "label": "GPT-4o"}],
        )
        await session.commit()
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        with pytest.raises(ValidationError):
            await svc.create_session(title="x", model=make_provider_model_id(pid, "openai/gpt-4o"))


async def test_create_session_malformed_provider_id_is_422(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        # A ``provider:`` prefix with a non-UUID id is unknown, not a crash.
        with pytest.raises(ValidationError):
            await svc.create_session(title="x", model="provider:not-a-uuid:openai/gpt-4o")


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


async def test_list_sessions_batches_message_counts_no_n_plus_one(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """The page's message counts come from ONE grouped query, not one per row (#396).

    Counts are asserted per session (correctness) AND the SELECT count during
    ``list_sessions`` is pinned to a constant (the sessions page + the grouped
    count), so a reintroduced per-row ``count_messages`` fails here.
    """
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        repo = MessageRepository(session, world.tenant_a)
        expected: dict[str, int] = {}
        for i in range(5):
            created = await svc.create_session(title=f"s{i}", model=None)
            for j in range(i):
                await repo.add(
                    session_id=created.session.id, role=MessageRole.USER, content=f"m{j}"
                )
            expected[f"s{i}"] = i
        await session.commit()

        select_count = 0

        def _count_selects(  # noqa: ANN001 — SQLAlchemy event signature
            conn, cursor, statement, parameters, context, executemany
        ) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        sync_engine = session.get_bind()  # the sync Engine driving the async session
        event.listen(sync_engine, "before_cursor_execute", _count_selects)
        try:
            page = await svc.list_sessions(cursor=None, limit=20)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _count_selects)

    assert {v.session.title: v.message_count for v in page.items} == expected
    # One page query + one grouped count — NOT 1 + N. A small buffer (<=3) keeps
    # the pin robust to an extra cursor/metadata query without hiding an N+1
    # (which would cost 6 SELECTs for these 5 sessions).
    assert select_count <= 3, f"expected a batched count, saw {select_count} SELECTs"


async def test_count_for_sessions_excludes_foreign_tenant_rows(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """INV-1: the batched count never counts (or even names) another tenant's
    sessions — a tenant-A repository asked about a tenant-B session id returns
    no entry for it, so its messages can't leak into a count."""
    world, factory = world_and_factory
    async with factory() as session:
        a_repo = ChatSessionRepository(session, world.tenant_a)
        b_repo = ChatSessionRepository(session, world.tenant_b)
        a_session = await a_repo.create(owner_id=world.alice, model="anthropic/claude-opus-4.8")
        b_session = await b_repo.create(owner_id=world.carol, model="anthropic/claude-opus-4.8")
        await MessageRepository(session, world.tenant_a).add(
            session_id=a_session.id, role=MessageRole.USER, content="a"
        )
        await MessageRepository(session, world.tenant_b).add(
            session_id=b_session.id, role=MessageRole.USER, content="b"
        )
        await session.commit()

        counts = await a_repo.count_for_sessions([a_session.id, b_session.id])

    assert counts == {a_session.id: 1}
    assert b_session.id not in counts


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


async def test_send_threads_users_custom_instructions(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """The asking user's stored custom instructions ride the SendResult to the runtime."""
    world, factory = world_and_factory
    async with factory() as session:
        # Alice sets custom instructions on her own preferences row.
        await UserPreferenceRepository(session, world.tenant_a).set_custom_instructions(
            world.alice, "You are Alice's assistant."
        )
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        await session.commit()
        result = await svc.send_message(
            created.session.id, content="hi", model=None, backplane=InMemoryBackplane()
        )
        await session.commit()
    assert result is not None
    assert result.custom_instructions == "You are Alice's assistant."


async def test_send_without_preferences_has_no_custom_instructions(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """A user with no preferences row sends with custom_instructions=None (ad-hoc)."""
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        await session.commit()
        result = await svc.send_message(
            created.session.id, content="hi", model=None, backplane=InMemoryBackplane()
        )
        await session.commit()
    assert result is not None
    assert result.custom_instructions is None


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


async def test_send_hands_full_history_to_runtime_not_a_fixed_slice(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """#424 third re-review, major 2: send_message no longer pre-slices history to
    a fixed count — it hands ALL prior messages to the runtime, where the
    token-budgeted assembler decides what fits. Proves the service path, not just
    assemble_context directly."""
    world, factory = world_and_factory
    backplane = InMemoryBackplane()
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        repo = MessageRepository(session, world.tenant_a)
        # 30 prior short turns — beyond the old fixed-20 slice that used to drop them.
        for i in range(30):
            role = MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT
            await repo.add(session_id=created.session.id, role=role, content=f"turn {i}")
        await session.commit()

        result = await svc.send_message(
            created.session.id, content="the newest question", model=None, backplane=backplane
        )
        await session.commit()

    assert result is not None
    # Every one of the 30 prior turns rides the SendResult to the runtime — none
    # were silently dropped before the assembler saw them.
    assert len(result.history) == 30
    assert [m.content for m in result.history] == [f"turn {i}" for i in range(30)]


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


async def test_list_messages_hydrates_tool_invocations(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """An assistant message carries its governed tool trace; others carry [] (#377)."""
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        repo = MessageRepository(session, world.tenant_a)
        user_msg = await repo.add(
            session_id=created.session.id, role=MessageRole.USER, content="list my docs"
        )
        answer = await repo.add(
            session_id=created.session.id, role=MessageRole.ASSISTANT, content="You have 13."
        )
        tools = ToolInvocationRepository(session, world.tenant_a)
        await tools.record(
            tool_name="list_documents",
            args_hash="h1",
            ok=True,
            duration_ms=12,
            session_id=created.session.id,
            message_id=answer.id,
            result_summary="13 documents",
            ordinal=0,
        )
        await tools.record(
            tool_name="run_python",
            args_hash="h2",
            ok=False,
            error="tool_denied",
            duration_ms=0,
            session_id=created.session.id,
            message_id=answer.id,
            ordinal=1,
        )
        await session.commit()

        page = await svc.list_messages(created.session.id, cursor=None, limit=20)
        assert page is not None
        by_id = {v.message.id: v for v in page.items}
        # The user message carries no tool trace.
        assert by_id[user_msg.id].tool_invocations == []
        # The assistant message carries BOTH invocations (success and denial) —
        # a denial is never silently dropped (CC-7) — in STRICT oldest-first
        # order. Both rows share one SQLite second-precision timestamp, so this
        # pins the per-message ordinal as the real ordering key (#397).
        got = by_id[answer.id].tool_invocations
        assert [t.tool_name for t in got] == ["list_documents", "run_python"]
        assert got[0].ok is True
        assert got[0].duration_ms == 12
        # The handler's result line round-trips (#377 "what it returned").
        assert got[0].result_summary == "13 documents"
        assert got[1].ok is False
        assert got[1].error == "tool_denied"
        assert got[1].result_summary is None


async def test_list_messages_orders_tool_invocations_by_ordinal_within_a_tie(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """Insertion order out of arrival order: the ordinal wins within a timestamp tie (#397)."""
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        repo = MessageRepository(session, world.tenant_a)
        answer = await repo.add(
            session_id=created.session.id, role=MessageRole.ASSISTANT, content="a"
        )
        tools = ToolInvocationRepository(session, world.tenant_a)
        # Recorded out of logical order — the ordinal, not insertion order or the
        # random UUID id, must drive what the caller sees.
        for name, ordinal in [("third", 2), ("first", 0), ("second", 1)]:
            await tools.record(
                tool_name=name,
                args_hash="h",
                ok=True,
                duration_ms=1,
                session_id=created.session.id,
                message_id=answer.id,
                ordinal=ordinal,
            )
        # Force the premise deterministically: pin every row to ONE created_at so
        # the primary sort key ties by construction (an insert crossing a second
        # boundary would otherwise legitimately order 'third' first and flake).
        await session.execute(
            sa_update(db_models.ToolInvocation).values(
                created_at=datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
            )
        )
        await session.commit()

        page = await svc.list_messages(created.session.id, cursor=None, limit=20)
        assert page is not None
        got = page.items[-1].tool_invocations
        assert [t.tool_name for t in got] == ["first", "second", "third"]


async def test_list_for_messages_excludes_foreign_tenant_rows(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """INV-1: a foreign-tenant invocation is invisible — even one whose
    ``message_id`` points at OUR message (the non-composite FK permits that
    hostile/corrupt shape, so the tenant predicate must do the work)."""
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        repo = MessageRepository(session, world.tenant_a)
        answer = await repo.add(
            session_id=created.session.id, role=MessageRole.ASSISTANT, content="a"
        )
        await ToolInvocationRepository(session, world.tenant_a).record(
            tool_name="ours",
            args_hash="h",
            ok=True,
            duration_ms=1,
            session_id=created.session.id,
            message_id=answer.id,
        )
        # Hostile shape: a TENANT-B invocation row referencing tenant A's message.
        await ToolInvocationRepository(session, world.tenant_b).record(
            tool_name="theirs",
            args_hash="h",
            ok=True,
            duration_ms=1,
            message_id=answer.id,
        )
        await session.commit()

        by_message = await ToolInvocationRepository(session, world.tenant_a).list_for_messages(
            [answer.id]
        )
        assert [t.tool_name for t in by_message.get(answer.id, [])] == ["ours"]


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


# --- #416: the send path consumes the rolling summary ------------------------


async def test_send_filters_summarized_history_and_threads_the_summary(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """With a summary covering the older turns, ``SendResult.history`` contains
    only NEWER messages, and the summary text + evidence ids ride along for
    the runtime (#416, ADR-0016 §3.2)."""
    import uuid as _uuid

    from app.db.repositories import MessageRepository, SessionSummaryRepository
    from app.domain.entities import MessageRole as _Role

    world, factory = world_and_factory
    doc_id, chunk_id = _uuid.uuid4(), _uuid.uuid4()
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        messages = MessageRepository(session, world.tenant_a)
        ids = []
        for i in range(6):
            m = await messages.add(
                session_id=created.session.id,
                role=_Role.USER if i % 2 == 0 else _Role.ASSISTANT,
                content=f"old turn {i}",
            )
            ids.append(m.id)
        summaries = SessionSummaryRepository(session, world.tenant_a)
        await summaries.upsert_evidence(created.session.id, evidence=[(doc_id, chunk_id)])
        row = await summaries.get_for_session(created.session.id)
        assert row is not None
        # Give the covered turns DISTINCT older timestamps (SQLite stamps are
        # second-resolution and the cursor's tie rule conservatively RESENDS
        # same-second peers — a real session spans seconds).
        from datetime import datetime as _dt

        from sqlalchemy import update as _upd

        from app.db import models as _models

        for i, mid in enumerate(ids):
            await session.execute(
                _upd(_models.Message)
                .where(_models.Message.id == mid)
                .values(created_at=_dt(2000, 1, 1, 0, i))
            )
        await session.commit()
        # Cover the first four turns.
        boundary = ids[3]
        rows = await messages.list_for_session(created.session.id)
        by_id = {m.id: m for m in rows}
        accepted, _row = await summaries.upsert_summary(
            created.session.id,
            summary="They discussed old turns 0-3.",
            covers_through_message_id=boundary,
            covered_created_at=by_id[boundary].created_at,
        )
        assert accepted is True
        await session.commit()
        result = await svc.send_message(
            created.session.id, content="follow-up", model=None, backplane=InMemoryBackplane()
        )
        await session.commit()
    assert result is not None
    assert result.summary == "They discussed old turns 0-3."
    assert result.evidence == ((doc_id, chunk_id),)
    # History = only turns AFTER the coverage boundary (4, 5) — never the
    # summarized ones.
    contents = [m.content for m in result.history]
    assert contents == ["old turn 4", "old turn 5"]


async def test_send_without_summary_row_is_unchanged(
    world_and_factory: tuple[_World, async_sessionmaker[AsyncSession]],
) -> None:
    """AC-4 shape: no summary row ⇒ full history, no summary, no evidence —
    byte-for-byte today's behavior."""
    world, factory = world_and_factory
    async with factory() as session:
        svc = _service(session, tenant_id=world.tenant_a, owner_id=world.alice)
        created = await svc.create_session(title="t", model=None)
        await session.commit()
        result = await svc.send_message(
            created.session.id, content="hi", model=None, backplane=InMemoryBackplane()
        )
        await session.commit()
    assert result is not None
    assert result.summary is None
    assert result.evidence == ()
