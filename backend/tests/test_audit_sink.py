"""Audit sink service — the one injectable ``emit(...)`` (issue #23, spec 0004 §2.4).

The headline is **INV-6**: emitting persists an event carrying *all* required
fields, and an emit missing a required field is **rejected before the write**
(nothing reaches the table). These run against an in-memory async SQLite schema
(offline-safe, like the #44 repository tests) so no Postgres is needed; the
append-only DB grant is exercised by the live migration test, not here.

The sink is deliberately thin: it composes the taxonomy/envelope policy
(``domain/audit``) with the existing append-only ``AuditEventRepository``
(``db/`` — reused, not duplicated). It is the single seam every later feature
calls to satisfy the "auditable" mission filter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import AuditEventRepository
from app.domain.audit import AuditAction, AuditActor, AuditEnvelopeError
from app.domain.entities import AuditOutcome
from app.services.audit import AuditSink

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


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
async def tenant_id(session: AsyncSession) -> uuid.UUID:
    """A provisioned tenant to scope the sink to (FK target for audit rows)."""
    from app.db.repositories import TenantRepository

    tenant = await TenantRepository(session).create(name="Acme")
    return tenant.id


def _sink(session: AsyncSession, tenant_id: uuid.UUID) -> AuditSink:
    return AuditSink(AuditEventRepository(session, tenant_id))


# ---------------------------------------------------------------------------
# Happy path — emit persists a complete envelope.
# ---------------------------------------------------------------------------


async def test_emit_persists_event_with_all_required_fields(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """INV-6 (positive): a complete emit persists exactly one event, all fields set."""
    sink = _sink(session, tenant_id)
    actor = AuditActor.user(uuid.uuid4())
    resource = str(uuid.uuid4())

    event = await sink.emit(
        action=AuditAction.DOCUMENT_UPLOADED,
        actor=actor,
        resource_type="document",
        resource_id=resource,
        outcome=AuditOutcome.ALLOWED,
        request_id="req-123",
        source_ip="203.0.113.7",
        metadata={"size_bytes": 42},
    )

    # The returned domain event carries every required field (spec 0004 §2.4).
    assert event.id is not None
    assert event.ts is not None
    assert event.tenant_id == tenant_id
    assert event.actor_id == actor.actor_id
    assert event.action == "document.uploaded"
    assert event.resource_type == "document"
    assert event.resource_id == resource
    assert event.outcome is AuditOutcome.ALLOWED
    assert event.request_id == "req-123"
    assert event.source_ip == "203.0.113.7"
    assert event.metadata == {"size_bytes": 42}

    # And it is the only row, readable back through the same repository.
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert [e.id for e in recent] == [event.id]


async def test_emit_accepts_system_actor(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """A system actor persists with a null actor_id (spec 0004 §2.4)."""
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action=AuditAction.ANSWER_GENERATED,
        actor=AuditActor.system(),
        resource_type="message",
        outcome=AuditOutcome.ALLOWED,
    )
    assert event.actor_id is None


async def test_emit_accepts_anonymous_actor_for_login_failed(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """A failed login has no authenticated actor → anonymous, null actor_id."""
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action=AuditAction.AUTH_LOGIN_FAILED,
        actor=AuditActor.anonymous(),
        resource_type="session",
        outcome=AuditOutcome.DENIED,
    )
    assert event.actor_id is None
    assert event.outcome is AuditOutcome.DENIED


async def test_emit_accepts_taxonomy_action_string(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """A caller may pass the action as a taxonomy string, not only the enum."""
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action="permission.denied",
        actor=AuditActor.user(uuid.uuid4()),
        resource_type="document",
        outcome=AuditOutcome.DENIED,
    )
    assert event.action == "permission.denied"


async def test_emit_defaults_metadata_to_empty_dict(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action=AuditAction.AUTH_LOGIN,
        actor=AuditActor.user(uuid.uuid4()),
        resource_type="session",
        outcome=AuditOutcome.ALLOWED,
    )
    assert event.metadata == {}


# ---------------------------------------------------------------------------
# INV-6 (negative) — a missing required field is rejected *before* the write.
# ---------------------------------------------------------------------------


async def test_inv6_missing_resource_type_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """A blank required field raises and persists nothing (fail-closed)."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="",  # required field missing
            outcome=AuditOutcome.ALLOWED,
        )
    # Nothing was written — the table is empty.
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_unknown_action_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """An action outside the taxonomy is rejected before any write (deny by default)."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action="totally.made.up",
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


# ---------------------------------------------------------------------------
# Append-only at the API surface — the sink exposes no mutation path.
# ---------------------------------------------------------------------------


def test_sink_has_no_update_or_delete_method() -> None:
    """Append-only by construction: emit only, no update/delete (spec 0004 §2.4)."""
    names = set(dir(AuditSink))
    assert "emit" in names
    assert "update" not in names
    assert "delete" not in names
