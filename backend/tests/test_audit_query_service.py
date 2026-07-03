"""Audit-query service tests — filter + keyset pagination + provenance (#85).

Exercises :class:`~app.services.audit_query_service.AuditQueryService` directly
against an offline in-memory SQLite schema (no Postgres), the same pattern the
sink/repository tests use. The service is the read counterpart of the one audit
sink (#23): it queries the append-only ``audit_events`` table, **tenant-scoped**
(INV-1), with the contract's filters (actor / event_type / resource_id /
from / to) + keyset pagination, and maps each stored event to the contract's
``AuditEvent`` shape — including the ``provenance`` projection (candidate
allow/exclude dispositions + the raw recorded payload).

The HTTP role-gate (INV-5 → 403) and the end-to-end wire shape are covered in
``test_audit_query_api``; this module proves the query/mapping behavior in
isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import AuditEventRepository, TenantRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome
from app.services.audit import AuditSink
from app.services.audit_query_service import AuditQueryService

# Importing models registers them on Base.metadata for create_all.
import app.db.models  # noqa: F401  isort: skip


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A StaticPool SQLite engine + schema (offline-safe)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def two_tenants(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """Provision two tenants (FK targets for audit rows). Returns (A, B)."""
    async with sessionmaker() as session:
        a = await TenantRepository(session).create(name="Acme")
        b = await TenantRepository(session).create(name="Globex")
        await session.commit()
        return a.id, b.id


async def _emit(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    action: AuditAction | str,
    actor: AuditActor,
    resource_type: str = "document",
    resource_id: str | None = None,
    outcome: AuditOutcome = AuditOutcome.ALLOWED,
    metadata: dict[str, object] | None = None,
) -> uuid.UUID:
    """Emit one audit event through the real sink; return its id."""
    async with sessionmaker() as session:
        sink = AuditSink(AuditEventRepository(session, tenant_id))
        event = await sink.emit(
            action=action,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id or str(uuid.uuid4()),
            outcome=outcome,
            request_id="req-test",
            source_ip="203.0.113.7",
            metadata=metadata,
        )
        await session.commit()
        return event.id


def _service(session: AsyncSession, tenant_id: uuid.UUID) -> AuditQueryService:
    return AuditQueryService(session, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Tenant scope (INV-1) — a query never crosses the tenant boundary.
# ---------------------------------------------------------------------------


async def test_query_returns_only_own_tenant_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """INV-1: tenant A's query never sees tenant B's events."""
    tenant_a, tenant_b = two_tenants
    mine = await _emit(
        sessionmaker, tenant_id=tenant_a, action=AuditAction.AUTH_LOGIN, actor=AuditActor.system()
    )
    await _emit(
        sessionmaker, tenant_id=tenant_b, action=AuditAction.AUTH_LOGIN, actor=AuditActor.system()
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    ids = {e.id for e in page.items}
    assert ids == {mine}


async def test_resource_filter_does_not_leak_cross_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A shared resource_id in another tenant is still excluded (INV-1 + filter)."""
    tenant_a, tenant_b = two_tenants
    shared_resource = str(uuid.uuid4())
    mine = await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_VIEWED,
        actor=AuditActor.system(),
        resource_id=shared_resource,
    )
    await _emit(
        sessionmaker,
        tenant_id=tenant_b,
        action=AuditAction.DOCUMENT_VIEWED,
        actor=AuditActor.system(),
        resource_id=shared_resource,
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query(resource_id=shared_resource)
    assert [e.id for e in page.items] == [mine]


# ---------------------------------------------------------------------------
# Filters — actor / event_type / resource_id / from / to.
# ---------------------------------------------------------------------------


async def test_filter_by_event_type(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    login = await _emit(
        sessionmaker, tenant_id=tenant_a, action=AuditAction.AUTH_LOGIN, actor=AuditActor.system()
    )
    await _emit(
        sessionmaker, tenant_id=tenant_a, action=AuditAction.AUTH_LOGOUT, actor=AuditActor.system()
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query(event_type="auth.login")
    assert [e.id for e in page.items] == [login]


async def test_filter_by_actor_user_id(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    mine = await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_VIEWED,
        actor=AuditActor.user(alice),
    )
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_VIEWED,
        actor=AuditActor.user(bob),
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query(actor=str(alice))
    assert [e.id for e in page.items] == [mine]


async def test_filter_by_actor_system_matches_null_actor(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A null-actor row (system/anonymous) is selected by actor=system."""
    tenant_a, _ = two_tenants
    sys_event = await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.ANSWER_GENERATED,
        actor=AuditActor.system(),
        resource_type="message",
    )
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_VIEWED,
        actor=AuditActor.user(uuid.uuid4()),
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query(actor="system")
    assert [e.id for e in page.items] == [sys_event]


async def test_filter_by_resource_id(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    target = str(uuid.uuid4())
    mine = await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_DOWNLOADED,
        actor=AuditActor.system(),
        resource_id=target,
    )
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_DOWNLOADED,
        actor=AuditActor.system(),
        resource_id=str(uuid.uuid4()),
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query(resource_id=target)
    assert [e.id for e in page.items] == [mine]


async def test_unknown_actor_uuid_returns_empty(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_VIEWED,
        actor=AuditActor.user(uuid.uuid4()),
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query(actor=str(uuid.uuid4()))
    assert page.items == []


# ---------------------------------------------------------------------------
# Ordering + pagination — newest → oldest, keyset cursor, no duplicates.
# ---------------------------------------------------------------------------


async def test_orders_newest_first(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    ids = []
    for _ in range(3):
        ids.append(
            await _emit(
                sessionmaker,
                tenant_id=tenant_a,
                action=AuditAction.DOCUMENT_VIEWED,
                actor=AuditActor.system(),
            )
        )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    # Newest first: each event's ts is non-increasing down the page.
    timestamps = [e.ts for e in page.items]
    assert timestamps == sorted(timestamps, reverse=True)
    assert len(page.items) == 3


async def test_cursor_pagination_walks_all_pages_without_duplicates(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    created: set[uuid.UUID] = set()
    for _ in range(5):
        created.add(
            await _emit(
                sessionmaker,
                tenant_id=tenant_a,
                action=AuditAction.DOCUMENT_VIEWED,
                actor=AuditActor.system(),
            )
        )
    seen: set[uuid.UUID] = set()
    cursor: str | None = None
    pages = 0
    while True:
        async with sessionmaker() as session:
            page = await _service(session, tenant_a).query(cursor=cursor, limit=2)
        assert len(page.items) <= 2
        for event in page.items:
            assert event.id not in seen  # no duplicates across pages
            seen.add(event.id)
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages < 10  # guard against a non-terminating cursor
    assert seen == created


async def test_invalid_cursor_is_rejected(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    from app.core.errors import ValidationError

    tenant_a, _ = two_tenants
    async with sessionmaker() as session:
        with pytest.raises(ValidationError):
            await _service(session, tenant_a).query(cursor="not-a-real-cursor!!")


# ---------------------------------------------------------------------------
# Provenance mapping — candidates (allow/exclude) + raw payload.
# ---------------------------------------------------------------------------


async def test_provenance_carries_raw_metadata(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    payload = {"model": "anthropic/claude-opus-4.8", "query_hash": "abc123"}
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.ANSWER_GENERATED,
        actor=AuditActor.system(),
        resource_type="message",
        metadata=dict(payload),
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    event = page.items[0]
    assert event.provenance.raw == payload
    # No candidate dispositions recorded → empty list, never absent (required).
    assert event.provenance.candidates == []


async def test_provenance_maps_explicit_candidate_dispositions(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """An event that recorded explicit allow/exclude candidates maps them through."""
    tenant_a, _ = two_tenants
    doc_in = str(uuid.uuid4())
    doc_out = str(uuid.uuid4())
    metadata = {
        "candidates": [
            {"resource_id": doc_in, "disposition": "allow", "reason": "in allow-set", "score": 0.9},
            {"resource_id": doc_out, "disposition": "exclude", "reason": "owner mismatch"},
        ]
    }
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="retrieval",
        metadata=metadata,
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    candidates = page.items[0].provenance.candidates
    by_id = {c.resource_id: c for c in candidates}
    assert by_id[doc_in].disposition == "allow"
    assert by_id[doc_in].reason == "in allow-set"
    assert by_id[doc_in].score == 0.9
    assert by_id[doc_out].disposition == "exclude"
    assert by_id[doc_out].reason == "owner mismatch"
    assert by_id[doc_out].score is None


async def test_provenance_synthesizes_allow_candidates_from_document_ids(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A retrieval event recording retrieved document_ids yields allow-candidates."""
    tenant_a, _ = two_tenants
    docs = [str(uuid.uuid4()), str(uuid.uuid4())]
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="retrieval",
        metadata={"document_ids": docs, "hit_count": 2},
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    candidates = page.items[0].provenance.candidates
    assert {c.resource_id for c in candidates} == set(docs)
    assert all(c.disposition == "allow" for c in candidates)


async def test_answer_event_with_document_ids_yields_allow_candidates(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """An answer.generated event recording cited document_ids yields allow
    candidates — the projection the frontend "Answers cited" KPI reads to count
    a grounded answer as cited (#249)."""
    tenant_a, _ = two_tenants
    docs = [str(uuid.uuid4())]
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.ANSWER_GENERATED,
        actor=AuditActor.user(uuid.uuid4()),
        resource_type="message",
        metadata={"document_ids": docs, "citation_count": 1},
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    candidates = page.items[0].provenance.candidates
    assert [c.resource_id for c in candidates] == docs
    assert candidates[0].disposition == "allow"


# ---------------------------------------------------------------------------
# Event → contract mapping — actor label + resource_id passthrough.
# ---------------------------------------------------------------------------


async def test_user_actor_renders_as_user_id(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    alice = uuid.uuid4()
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.DOCUMENT_VIEWED,
        actor=AuditActor.user(alice),
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    assert page.items[0].actor == str(alice)


async def test_null_actor_renders_as_system_label(
    sessionmaker: async_sessionmaker[AsyncSession],
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    await _emit(
        sessionmaker,
        tenant_id=tenant_a,
        action=AuditAction.ANSWER_GENERATED,
        actor=AuditActor.system(),
        resource_type="message",
    )
    async with sessionmaker() as session:
        page = await _service(session, tenant_a).query()
    assert page.items[0].actor == "system"
