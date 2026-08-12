"""Transaction boundary for durable denied-access audit rows.

Successful audit rows share the action transaction. An authenticated denial is
different: its typed 403/404 ends the request without a success commit, so its
evidence needs a small independent transaction. This module keeps construction
and ownership of that relational transaction inside ``app.db`` (ADR-0004).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.repositories import AuditEventRepository
from app.db.tenant_context import bind_tenant


@asynccontextmanager
async def durable_audit_repository(
    request_session: AsyncSession,
    tenant_id: UUID,
) -> AsyncIterator[AuditEventRepository]:
    """Yield a repository in a fresh tenant-bound transaction and commit it.

    The new session is engine-bound, never connection-bound, so it cannot share
    the caller's open transaction. Requiring an :class:`AsyncEngine` rules out
    silently cloning a session bound to an already-open ``AsyncConnection``.
    RLS is rebound from the trusted, auth-resolved tenant in the new transaction.
    Any bind, sink, flush, or commit failure rolls back and propagates (INV-6).
    """
    bind = request_session.bind
    if not isinstance(bind, AsyncEngine):
        raise RuntimeError("Durable audit recording requires an engine-bound session.")
    factory = async_sessionmaker(
        bind=bind,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as audit_session:
        try:
            await bind_tenant(audit_session, tenant_id)
            yield AuditEventRepository(audit_session, tenant_id)
            await audit_session.commit()
        except Exception:
            await audit_session.rollback()
            raise


__all__ = ["durable_audit_repository"]
