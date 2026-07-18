"""Reusable sandbox-session domain/repository tests (ADR-0020, issue #457)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    SandboxSessionRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import Role, SandboxSessionStatus
from app.sandbox.runner import build_container_flags
from app.sandbox.service import PackagePolicyError, validate_requested_packages
from app.sandbox.spec import SandboxSessionSpec

import app.db.models  # noqa: F401  isort: skip


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
        async with factory() as value:
            yield value
    finally:
        await engine.dispose()


async def _chat(session: AsyncSession, *, tenant_name: str, email: str) -> tuple[object, ...]:
    tenant = await TenantRepository(session).create(name=tenant_name)
    owner = await UserRepository(session, tenant.id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    chat = await ChatSessionRepository(session, tenant.id).create(
        owner_id=owner.id, model="openrouter:test/model"
    )
    return tenant, owner, chat


async def test_one_durable_session_is_reused_per_chat(session: AsyncSession) -> None:
    tenant, owner, chat = await _chat(
        session, tenant_name="Acme", email="alice@acme.test"
    )
    chats = ChatSessionRepository(session, tenant.id)
    second_chat = await chats.create(owner_id=owner.id, model="openrouter:test/model")
    repo = SandboxSessionRepository(session, tenant.id)

    first = await repo.get_or_create(
        owner_id=owner.id, chat_session_id=chat.id, image_digest="python@sha256:abc"
    )
    reused = await repo.get_or_create(
        owner_id=owner.id, chat_session_id=chat.id, image_digest="python@sha256:abc"
    )
    isolated = await repo.get_or_create(
        owner_id=owner.id,
        chat_session_id=second_chat.id,
        image_digest="python@sha256:abc",
    )

    assert reused.id == first.id
    assert reused.generation == 1
    assert reused.status is SandboxSessionStatus.ACTIVE
    assert isolated.id != first.id


async def test_session_repository_is_tenant_scoped(session: AsyncSession) -> None:
    tenant_a, owner_a, chat_a = await _chat(
        session, tenant_name="Acme", email="alice@acme.test"
    )
    tenant_b, _, _ = await _chat(session, tenant_name="Globex", email="bob@globex.test")
    created = await SandboxSessionRepository(session, tenant_a.id).get_or_create(
        owner_id=owner_a.id,
        chat_session_id=chat_a.id,
        image_digest="python@sha256:abc",
    )

    assert await SandboxSessionRepository(session, tenant_b.id).get(created.id) is None


async def test_reset_advances_generation_and_close_is_durable(session: AsyncSession) -> None:
    tenant, owner, chat = await _chat(
        session, tenant_name="Acme", email="alice@acme.test"
    )
    repo = SandboxSessionRepository(session, tenant.id)
    created = await repo.get_or_create(
        owner_id=owner.id, chat_session_id=chat.id, image_digest="python@sha256:abc"
    )

    reset = await repo.advance_generation(created.id)
    assert reset is not None
    assert reset.generation == 2
    assert reset.status is SandboxSessionStatus.ACTIVE
    assert reset.closed_at is None

    closed = await repo.close(created.id)
    assert closed is not None
    assert closed.status is SandboxSessionStatus.CLOSED
    assert closed.closed_at is not None


async def test_error_state_recovers_with_a_fresh_generation(session: AsyncSession) -> None:
    tenant, owner, chat = await _chat(
        session, tenant_name="Acme", email="alice@acme.test"
    )
    repo = SandboxSessionRepository(session, tenant.id)
    created = await repo.get_or_create(
        owner_id=owner.id, chat_session_id=chat.id, image_digest="python@sha256:abc"
    )
    await repo.mark_error(created.id)

    recovered = await repo.get_or_create(
        owner_id=owner.id, chat_session_id=chat.id, image_digest="python@sha256:def"
    )

    assert recovered.id == created.id
    assert recovered.generation == 2
    assert recovered.status is SandboxSessionStatus.ACTIVE


def test_container_is_root_writable_offline_and_has_no_automatic_limits() -> None:
    spec = SandboxSessionSpec(
        sandbox_session_id=__import__("uuid").uuid4(),
        generation=1,
        image="python@sha256:abc",
        runtime="runsc",
    )
    flags = build_container_flags(spec)

    assert flags.network_mode == "none"
    assert flags.binds == ()
    assert flags.read_only_rootfs is False
    assert flags.user == "0:0"
    assert flags.cap_drop == ("ALL",)
    assert flags.security_opt == ("no-new-privileges:true",)
    assert flags.cpus is None
    assert flags.memory_bytes is None
    assert flags.pids_limit is None
    assert flags.wall_clock_seconds is None
    assert flags.output_bytes_cap is None


def test_package_policy_canonicalizes_allows_and_denies() -> None:
    assert validate_requested_packages(
        ("NumPy==2.1.0", "pandas[excel]>=2"),
        allowed=("numpy", "pandas"),
        denied=(),
    ) == ("numpy==2.1.0", "pandas[excel]>=2")

    assert validate_requested_packages(
        ("scipy==1.14.0",), allowed=("*",), denied=()
    ) == ("scipy==1.14.0",)

    with pytest.raises(PackagePolicyError, match="requests"):
        validate_requested_packages(
            ("requests==2.32.3",), allowed=("*",), denied=("requests",)
        )

    with pytest.raises(PackagePolicyError, match="not-a-requirement"):
        validate_requested_packages(
            ("not-a-requirement @ file:///host",), allowed=("*",), denied=()
        )
