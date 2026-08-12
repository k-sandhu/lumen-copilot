"""Owned transaction boundary for durable denied-access audit rows.

Successful audit rows share the action transaction.  An authenticated denial is
different: its typed 403/404 ends the request without a success commit, so its
evidence needs an independently owned transaction.  The provider in this module
is injected into the service-layer recorder; it never derives a Session from the
caller's bind (R1-001), and construction/lifecycle stay inside ``app.db`` per
ADR-0004.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db.repositories import AuditEventRepository
from app.db.tenant_context import bind_tenant


class UnsafeAuditTransactionTopology(RuntimeError):
    """The durable-audit provider shares the caller's engine/connection."""


class DurableAuditTransactions:
    """Tenant-bound transactions on an engine owned only by durable auditing.

    Production supplies a separately constructed engine with a bounded pool.
    Tests may supply another engine explicitly.  In both cases
    :meth:`assert_independent_from` makes accidental reuse of the caller engine or
    connection fail before the protected service performs a write.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        operation_timeout_seconds: float,
    ) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("Durable audit operation timeout must be positive.")
        self._engine = engine
        self._operation_timeout_seconds = operation_timeout_seconds
        self._factory = async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """The engine this provider owns (exposed for lifecycle/safety checks)."""
        return self._engine

    def assert_independent_from(self, request_session: AsyncSession) -> None:
        """Reject a provider that could reuse the caller's physical connection.

        The old implementation constructed a new Session from
        ``request_session.bind``.  That self-starved a size-one QueuePool and
        shared a physical transaction under StaticPool.  Injection removes that
        construction path; this identity guard catches a caller accidentally
        injecting its own engine (including a connection-bound Session) back in.
        """
        bind = request_session.bind
        caller_engine: AsyncEngine | None
        if isinstance(bind, AsyncEngine):
            caller_engine = bind
        elif isinstance(bind, AsyncConnection):
            caller_engine = bind.engine
        else:
            caller_engine = None
        if caller_engine is self._engine or (
            caller_engine is not None
            and caller_engine.sync_engine.pool is self._engine.sync_engine.pool
        ):
            raise UnsafeAuditTransactionTopology(
                "Durable audit transactions require a pool independent from the caller."
            )

    @asynccontextmanager
    async def repository(self, tenant_id: UUID) -> AsyncIterator[AuditEventRepository]:
        """Yield a canonical repository and commit only its bounded transaction.

        Acquisition, RLS binding, sink flush, and commit are all inside one
        explicit timeout.  Any failure rolls this transaction back and propagates
        (INV-6); caller state is unreachable from this provider.
        """
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._factory() as audit_session:
                try:
                    await bind_tenant(audit_session, tenant_id)
                    yield AuditEventRepository(audit_session, tenant_id)
                    await audit_session.commit()
                except BaseException:
                    await audit_session.rollback()
                    raise

    async def dispose(self) -> None:
        """Release the owned pool; idempotent under SQLAlchemy engine disposal."""
        await self._engine.dispose()


__all__ = ["DurableAuditTransactions", "UnsafeAuditTransactionTopology"]
