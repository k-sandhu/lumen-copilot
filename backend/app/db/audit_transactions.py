"""Owned transaction boundary for durable denied-access audit rows.

Successful audit rows share the action transaction. A trusted request/background
denial is different: its typed 403/404 or pre-execution refusal has no success
commit, so its evidence needs an independently owned transaction. The provider
in this module is injected into the service-layer recorder; it never derives a
Session from the caller's bind (R1-001), and construction/lifecycle stay inside
``app.db`` per ADR-0004.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db.repositories import AuditEventRepository
from app.db.tenant_context import bind_tenant
from app.domain.entities import AuditEvent


class UnsafeAuditTransactionTopology(RuntimeError):
    """The durable-audit provider shares the caller's engine/connection."""


class _AmbiguousCompletion(Exception):
    def __init__(self, error: BaseException) -> None:
        self.error = error


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

    async def _invalidate(self, audit_session: AsyncSession) -> None:
        """Discard a connection whose transaction outcome may be ambiguous."""
        # A timed-out COMMIT must never return its physical connection to the
        # pool as healthy.  ``invalidate`` closes/discards it; the following
        # reconciliation therefore runs through a fresh pool acquisition.
        async with asyncio.timeout(self._operation_timeout_seconds):
            await audit_session.invalidate()

    async def _attempt(
        self,
        tenant_id: UUID,
        event_id: UUID,
        operation: Callable[[AuditEventRepository], Awaitable[AuditEvent]],
    ) -> AuditEvent:
        """Run one bounded write+commit attempt without a detached commit task."""
        async with self._factory() as audit_session:
            committing = False
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    await bind_tenant(audit_session, tenant_id)
                    event = await operation(AuditEventRepository(audit_session, tenant_id))
                    if event.id != event_id:
                        raise RuntimeError(
                            "Durable audit operation returned a different idempotency key."
                        )
                    # Await COMMIT in this task.  asyncio.timeout cancels and
                    # waits for this coroutine to unwind before TimeoutError is
                    # raised, so no background commit survives the guard call.
                    committing = True
                    await audit_session.commit()
                    return event
            except (TimeoutError, DBAPIError, asyncio.CancelledError) as error:
                ambiguous = isinstance(error, TimeoutError) or (
                    committing
                    and (
                        isinstance(error, asyncio.CancelledError)
                        or isinstance(error, DBAPIError)
                        and error.connection_invalidated
                    )
                )
                if ambiguous:
                    # Shield cleanup from repeated caller cancellation, but await
                    # it to completion: no detached connection/commit task survives.
                    _, cancelled = await self._settle(self._invalidate(audit_session))
                    if cancelled:
                        error = asyncio.CancelledError()
                    raise _AmbiguousCompletion(error) from error
                await audit_session.rollback()
                raise
            except BaseException:
                await audit_session.rollback()
                raise

    async def _settle(
        self, operation: Awaitable[AuditEvent | None]
    ) -> tuple[AuditEvent | None, bool]:
        """Drain bounded cleanup/reconciliation even if the caller cancels again."""
        task = asyncio.ensure_future(operation)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        return result, cancelled

    async def _reconcile(
        self,
        tenant_id: UUID,
        event_id: UUID,
        operation: Callable[[AuditEventRepository], Awaitable[AuditEvent]],
    ) -> AuditEvent | None:
        """Read and validate an ambiguous attempt through a fresh transaction."""
        async with self._factory() as audit_session:
            try:
                async with asyncio.timeout(self._operation_timeout_seconds):
                    await bind_tenant(audit_session, tenant_id)
                    repository = AuditEventRepository(audit_session, tenant_id)
                    if await repository.get(event_id) is None:
                        await audit_session.rollback()
                        return None
                    # The exact same operation performs the repository's full
                    # canonical-payload equality check against the existing row.
                    # The no-op conflict insert is rolled back because this is a
                    # read/reconciliation transaction, never a second append.
                    event = await operation(repository)
                    if event.id != event_id:
                        raise RuntimeError(
                            "Durable audit reconciliation returned a different " "idempotency key."
                        )
                    await audit_session.rollback()
                    return event
            except TimeoutError:
                await self._invalidate(audit_session)
                raise
            except BaseException:
                await audit_session.rollback()
                raise

    async def execute_idempotent(
        self,
        tenant_id: UUID,
        event_id: UUID,
        operation: Callable[[AuditEventRepository], Awaitable[AuditEvent]],
    ) -> AuditEvent:
        """Persist one semantic denial despite an ambiguous COMMIT outcome.

        ``event_id`` is allocated once by the trusted guard recorder and reused
        for at most one retry.  After every timeout, a new tenant-bound session
        reads the key and validates the complete canonical payload.  A committed
        row is success, an absent first attempt is retried once, and an absent
        second attempt fails closed. Invalidated COMMIT connections and in-flight
        cancellation use the same protocol; deterministic pre-COMMIT errors do not.
        """
        try:
            return await self._attempt(tenant_id, event_id, operation)
        except _AmbiguousCompletion as completion:
            original_error = completion.error

            async def recover() -> AuditEvent:
                reconciled = await self._reconcile(tenant_id, event_id, operation)
                if reconciled is not None:
                    return reconciled
                try:
                    return await self._attempt(tenant_id, event_id, operation)
                except _AmbiguousCompletion as retry:
                    reconciled = await self._reconcile(tenant_id, event_id, operation)
                    if reconciled is None:
                        raise retry.error from retry
                    return reconciled

            reconciled, cancelled = await self._settle(recover())
            if cancelled or isinstance(original_error, asyncio.CancelledError):
                raise asyncio.CancelledError from completion
            assert reconciled is not None
            return reconciled

    async def dispose(self) -> None:
        """Release the owned pool; idempotent under SQLAlchemy engine disposal."""
        await self._engine.dispose()


__all__ = ["DurableAuditTransactions", "UnsafeAuditTransactionTopology"]
