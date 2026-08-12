"""Async SQLAlchemy engine + session factory.

Owns the process-wide async engine (lazy, singleton) and an
``async_sessionmaker``. Async end-to-end (backend/AGENTS.md "Async") — the
engine is created from the ``postgresql+asyncpg://`` URL in settings and nothing
here blocks the event loop. The engine is created lazily so importing this
module (e.g. in tests, or by Alembic) does not open a connection, and is
disposed cleanly on shutdown via the app lifespan.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.db.audit_transactions import DurableAuditTransactions
from app.db.tenant_context import bind_tenant

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_durable_audit: DurableAuditTransactions | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
        )
    return _engine


def get_sessionmaker(
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


def get_durable_audit_transactions(
    settings: Settings | None = None,
) -> DurableAuditTransactions:
    """Return the process-owned durable-audit transaction provider.

    This engine is deliberately separate from :func:`get_engine`: denial paths
    often hold a request-pool connection after their visibility SELECT.  A
    dedicated bounded pool prevents circular acquisition at request-pool
    capacity and guarantees that its commit cannot touch caller work (R1-001).
    """
    global _durable_audit
    if _durable_audit is None:
        settings = settings or get_settings()
        engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.audit_db_pool_size,
            max_overflow=0,
            pool_timeout=settings.audit_db_pool_timeout_seconds,
            future=True,
        )
        _durable_audit = DurableAuditTransactions(
            engine,
            operation_timeout_seconds=settings.audit_db_operation_timeout_seconds,
        )
    return _durable_audit


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session within a transactional scope.

    Commits on success, rolls back on exception, always closes. Used outside
    the request path (e.g. tasks, startup checks); request handlers get a
    session from the FastAPI dependency in ``app.api.deps`` instead.

    Binds **no** tenant GUC: a caller doing tenant-scoped work must use
    :func:`tenant_session_scope` (or set the bypass sentinel itself) so the RLS
    backstop (#17) is armed. Tenant-agnostic/system callers (the seed sets the
    bypass; ``ping`` issues a non-scoped ``SELECT 1``) use this directly.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def tenant_session_scope(tenant_id: UUID) -> AsyncIterator[AsyncSession]:
    """Yield a transactional session with the RLS GUC bound to ``tenant_id`` (#17).

    The off-request analogue of the ``current_tenant`` dependency: opens a
    session, binds ``app.tenant_id`` for the transaction (so the Postgres RLS
    backstop permits exactly this tenant's rows — a no-op off Postgres), then
    commits on success / rolls back on error. Celery tasks doing tenant-scoped
    DB work use this so they run *as* their tenant, mirroring the request path.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            await bind_tenant(session, tenant_id)
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ping() -> None:
    """Reachability check: open a session and run ``SELECT 1``.

    Kept here (not in the api layer) so SQL stays inside ``db/`` per the
    boundary table; the readiness probe composes this, it does not write SQL.
    """
    from sqlalchemy import text

    factory = get_sessionmaker()
    async with factory() as session:
        await session.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    """Dispose the engine and reset module state (called on app shutdown)."""
    global _engine, _sessionmaker, _durable_audit
    if _durable_audit is not None:
        await _durable_audit.dispose()
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    _durable_audit = None
