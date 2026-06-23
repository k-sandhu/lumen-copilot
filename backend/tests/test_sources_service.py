"""SourcesService tests — CRUD, ownership, audit, SSRF mapping (#20, ADR-0009 §5).

Drives the service against an **offline** in-memory SQLite DB (no Postgres) with
faked enqueue (no broker). Covers the add/list/sync/delete happy paths, the audit
events (``source.added`` / ``synced`` / ``deleted``), the deny-by-default
ownership/tenancy collapse to ``None``/``False`` (→ 404 at the router,
INV-1/INV-2), and the SSRF/validation rejections (``ConnectorConfigError`` →
422). The connector's request-path ``validate_config`` does a synchronous
scheme/host check only (no DNS) for IP-literal URLs, so these stay offline.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import ValidationError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    SourceRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.audit import AuditAction
from app.domain.entities import Role, SourceStatus
from app.services.audit import AuditSink
from app.services.sources_service import SourcesService

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata

_PUBLIC = "93.184.216.34"


class _Seeded:
    def __init__(
        self, *, tenant_a: uuid.UUID, tenant_b: uuid.UUID, alice: uuid.UUID, bob: uuid.UUID
    ) -> None:
        self.tenant_a = tenant_a
        self.tenant_b = tenant_b
        self.alice = alice  # tenant A owner under test
        self.bob = bob  # tenant A, *other* owner


@pytest_asyncio.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
            tenant_a = await TenantRepository(seed).create(name="Acme")
            tenant_b = await TenantRepository(seed).create(name="Globex")
            alice = await UserRepository(seed, tenant_a.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            bob = await UserRepository(seed, tenant_a.id).create(
                email="bob@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            await seed.commit()
        factory.seeded = _Seeded(  # type: ignore[attr-defined]
            tenant_a=tenant_a.id, tenant_b=tenant_b.id, alice=alice.id, bob=bob.id
        )
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> _Seeded:
    return sessionmaker.seeded  # type: ignore[attr-defined, no-any-return]


@pytest.fixture(autouse=True)
def no_broker(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Replace the sync enqueue with a recorder (offline, no broker)."""
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        "app.tasks.enqueue_source_sync",
        lambda tid, sid: calls.append((tid, sid)),
    )
    return calls


def _service(session: AsyncSession, *, tenant_id: uuid.UUID, owner_id: uuid.UUID) -> SourcesService:
    return SourcesService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        audit=AuditSink(AuditEventRepository(session, tenant_id)),
        request_id="req-1",
        source_ip="203.0.113.5",
    )


# --- add --------------------------------------------------------------------


async def test_add_creates_pending_source_and_audits(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        svc = _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice)
        source = await svc.add(source_type="web", url=f"http://{_PUBLIC}/page")
        await session.commit()
        assert source.status == SourceStatus.PENDING
        assert source.owner_id == seeded.alice
        assert source.config["url"] == f"http://{_PUBLIC}/page"
        # A backing collection id is stored internally (not exposed on the wire).
        assert "collection_id" in source.config

        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
        actions = {e.action for e in events}
        assert AuditAction.SOURCE_ADDED.value in actions


async def test_add_rejects_ssrf_url(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        svc = _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice)
        with pytest.raises(ValidationError) as exc:
            await svc.add(source_type="web", url="http://127.0.0.1/secret")
        assert exc.value.code == "url_blocked"
        assert exc.value.status == 422


async def test_add_rejects_unknown_type(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        svc = _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice)
        with pytest.raises(ValidationError) as exc:
            await svc.add(source_type="dropbox", url=f"http://{_PUBLIC}/x")
        assert exc.value.code == "unsupported_source_type"


# --- list -------------------------------------------------------------------


async def test_list_is_owner_scoped(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).add(
            source_type="web", url=f"http://{_PUBLIC}/alice"
        )
        await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.bob).add(
            source_type="web", url=f"http://{_PUBLIC}/bob"
        )
        await session.commit()

        page = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).list_page(
            cursor=None, limit=20
        )
        assert len(page.items) == 1
        assert page.items[0].owner_id == seeded.alice


async def test_list_rejects_bad_cursor(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        svc = _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice)
        with pytest.raises(ValidationError):
            await svc.list_page(cursor="not-a-cursor!!", limit=20)


# --- get / ownership (INV-1/INV-2) ------------------------------------------


async def test_get_other_owner_returns_none(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        bob_src = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.bob).add(
            source_type="web", url=f"http://{_PUBLIC}/bob"
        )
        await session.commit()
        # Alice cannot see Bob's source — None (→ 404), not a 403.
        seen = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).get(
            bob_src.id
        )
        assert seen is None


async def test_get_cross_tenant_returns_none(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        src = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).add(
            source_type="web", url=f"http://{_PUBLIC}/a"
        )
        await session.commit()
        # A different tenant's repository never sees the row.
        seen = await _service(session, tenant_id=seeded.tenant_b, owner_id=seeded.alice).get(src.id)
        assert seen is None


# --- sync -------------------------------------------------------------------


async def test_resync_enqueues_and_moves_to_syncing(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    no_broker: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    async with sessionmaker() as session:
        src = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).add(
            source_type="web", url=f"http://{_PUBLIC}/a"
        )
        await session.commit()
        no_broker.clear()

        result = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).resync(
            src.id
        )
        await session.commit()
        assert result is not None
        source, enqueued = result
        assert enqueued is True
        assert source.status == SourceStatus.SYNCING
        assert no_broker == [(seeded.tenant_a, src.id)]

        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
        assert AuditAction.SOURCE_SYNCED.value in {e.action for e in events}


async def test_resync_already_syncing_is_noop(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: _Seeded,
    no_broker: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    async with sessionmaker() as session:
        src = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).add(
            source_type="web", url=f"http://{_PUBLIC}/a"
        )
        await SourceRepository(session, seeded.tenant_a).update_status(
            src.id, status=SourceStatus.SYNCING
        )
        await session.commit()
        no_broker.clear()

        result = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).resync(
            src.id
        )
        assert result is not None
        _source, enqueued = result
        assert enqueued is False
        assert no_broker == []  # no new sync enqueued


async def test_resync_other_owner_returns_none(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        bob_src = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.bob).add(
            source_type="web", url=f"http://{_PUBLIC}/bob"
        )
        await session.commit()
        result = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).resync(
            bob_src.id
        )
        assert result is None


# --- delete -----------------------------------------------------------------


async def test_delete_removes_and_audits(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        src = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).add(
            source_type="web", url=f"http://{_PUBLIC}/a"
        )
        await session.commit()

        ok = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).delete(
            src.id
        )
        await session.commit()
        assert ok is True
        assert await SourceRepository(session, seeded.tenant_a).get(src.id) is None

        events = await AuditEventRepository(session, seeded.tenant_a).list_recent()
        assert AuditAction.SOURCE_DELETED.value in {e.action for e in events}


async def test_delete_other_owner_returns_false(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: _Seeded
) -> None:
    async with sessionmaker() as session:
        bob_src = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.bob).add(
            source_type="web", url=f"http://{_PUBLIC}/bob"
        )
        await session.commit()
        ok = await _service(session, tenant_id=seeded.tenant_a, owner_id=seeded.alice).delete(
            bob_src.id
        )
        assert ok is False
        # Bob's source still exists.
        assert await SourceRepository(session, seeded.tenant_a).get(bob_src.id) is not None
