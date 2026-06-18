"""Audit sink dependency wiring (issue #23).

The sink is the seam later features inject. ``auth/`` tenant resolution is a
reserved seam (CC-3), so the dependency yields a **factory** keyed on the
resolved tenant id: a feature that already has ``current_tenant`` calls
``make_audit_sink(tenant_id)`` to get a request-scoped, tenant-scoped sink over
the same DB session. This test pins that the factory builds a usable sink
bound to the tenant it was handed — no live datastore required.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import make_audit_sink_factory
from app.db.base import Base
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.services.audit import AuditSink

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
        async with factory() as sess:
            yield sess
    finally:
        await engine.dispose()


async def test_factory_builds_a_tenant_scoped_sink(session: AsyncSession) -> None:
    """The dependency yields a factory that builds a sink scoped to a tenant."""
    from app.db.repositories import TenantRepository

    tenant = await TenantRepository(session).create(name="Acme")

    make_sink = make_audit_sink_factory(session)
    sink = make_sink(tenant.id)
    assert isinstance(sink, AuditSink)

    event = await sink.emit(
        action=AuditAction.AUTH_LOGIN,
        actor=AuditActor.user(uuid.uuid4()),
        resource_type="session",
        outcome=AuditOutcome.ALLOWED,
    )
    assert event.tenant_id == tenant.id
