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

import asyncio
import pathlib
import uuid
from collections.abc import AsyncIterator
from tempfile import TemporaryDirectory
from time import monotonic

import pytest
import pytest_asyncio
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool

from app.db import models
from app.db.audit_transactions import (
    DurableAuditTransactions,
    UnsafeAuditTransactionTopology,
)
from app.db.base import Base
from app.db.repositories import AuditEventRepository, TenantRepository
from app.domain.audit import AuditAction, AuditActor, AuditEnvelopeError
from app.domain.entities import AuditOutcome
from app.services.audit import (
    AuditSink,
    PermissionDeniedContext,
    PermissionDeniedRecorder,
    emit_permission_denied,
)
from tests._audit_helpers import RecordingDurableAuditTransactions, denial_recorder

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
        resource_id=str(uuid.uuid4()),
        request_id="req-sys",
        source_ip="203.0.113.7",
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
        resource_id=str(uuid.uuid4()),
        request_id="req-anon",
        source_ip="203.0.113.7",
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
        resource_id=str(uuid.uuid4()),
        request_id="req-str",
        source_ip="203.0.113.7",
    )
    assert event.action == "permission.denied"


@pytest.mark.parametrize(
    ("source", "expected_origin"),
    [("system", "system"), ("unknown", "unknown")],
)
async def test_permission_denial_helper_preserves_non_client_origin_and_safe_metadata(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    source: str,
    expected_origin: str,
) -> None:
    """Background/socket-origin denials use the same closed safe envelope."""
    actor_id = uuid.uuid4()
    resource_id = str(uuid.uuid4())
    event = await emit_permission_denied(
        _sink(session, tenant_id),
        event_id=uuid.uuid4(),
        actor=AuditActor.user(actor_id),
        resource_type="assistant",
        resource_id=resource_id,
        attempted_action="assistant.read",
        reason="not_visible",
        request_id="req-denied",
        source_ip=source,
    )

    assert event.actor_id == actor_id
    assert event.outcome is AuditOutcome.DENIED
    assert event.source_origin == expected_origin
    assert event.source_ip is None
    assert event.metadata == {
        "attempted_action": "assistant.read",
        "reason": "not_visible",
    }


async def test_durable_denial_propagates_canonical_sink_failure(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6: a sink failure aborts the denial response; it is never swallowed."""
    await session.commit()
    transactions = RecordingDurableAuditTransactions()
    recorder = denial_recorder(transactions, session, tenant_id)

    async def _fail_emit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(AuditSink, "emit", _fail_emit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await recorder.emit(
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="assistant",
            resource_id=str(uuid.uuid4()),
            attempted_action="assistant.read",
            reason="not_visible",
            request_id="req-failed-denial",
            source_ip="unknown",
        )

    assert await AuditEventRepository(session, tenant_id).list_recent() == []


@pytest.mark.parametrize("actor", [AuditActor.system(), AuditActor.anonymous()])
def test_explicit_non_user_denial_context_cannot_impersonate_a_user(
    actor: AuditActor,
) -> None:
    """Background/null attribution is valid, but never passes a user guard."""
    ledger = RecordingDurableAuditTransactions()
    context = PermissionDeniedContext(
        denial_recorder(ledger, object(), uuid.uuid4()),
        actor=actor,
        request_id="req-background",
        source_ip="system" if actor.is_system else "unknown",
    )
    with pytest.raises(ValueError, match="not bound to an authenticated user"):
        context.require_user()
    assert ledger.events == []


def test_denial_context_rejects_a_foreign_service_actor_before_the_guard() -> None:
    """A caller cannot pair trusted user A attribution with user B service state."""
    ledger = RecordingDurableAuditTransactions()
    actor_id = uuid.uuid4()
    context = PermissionDeniedContext(
        denial_recorder(ledger, object(), uuid.uuid4()),
        actor=AuditActor.user(actor_id),
        request_id="req-actor-match",
        source_ip="203.0.113.7",
    )
    assert context.require_user() == actor_id
    with pytest.raises(ValueError, match="must match"):
        context.assert_user(uuid.uuid4())
    assert ledger.events == []


async def test_staticpool_denial_does_not_commit_the_callers_pending_write(
    session: AsyncSession,
    tenant_id: uuid.UUID,
) -> None:
    """R1-001: a denial transaction must never share/commit caller work.

    The offline topology deliberately uses ``StaticPool``.  A second Session
    constructed from the caller's engine therefore receives the same physical
    connection.  This regression keeps a flushed tenant row pending while the
    denial is made durable, then rolls the caller back.  The pending row must not
    survive; if it does, the supposed independent audit commit committed the
    caller's transaction too.
    """
    await session.commit()  # make the denial tenant itself durable first
    pending = await TenantRepository(session).create(name="must roll back")
    await session.flush()

    bind = session.bind
    assert bind is not None
    transactions = DurableAuditTransactions(bind, operation_timeout_seconds=1)  # type: ignore[arg-type]
    with pytest.raises(UnsafeAuditTransactionTopology):
        PermissionDeniedRecorder(
            transactions,
            tenant_id=tenant_id,
            request_session=session,
        )
    await session.rollback()

    assert (
        await session.execute(select(models.Tenant).where(models.Tenant.id == pending.id))
    ).scalar_one_or_none() is None


async def test_connection_bound_caller_rejects_its_own_engine_before_service_work() -> None:
    """R1-001: a connection-bound caller cannot smuggle its engine into the provider."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.connect() as connection:
            session = AsyncSession(bind=connection)
            transactions = DurableAuditTransactions(engine, operation_timeout_seconds=1)
            with pytest.raises(UnsafeAuditTransactionTopology):
                PermissionDeniedRecorder(
                    transactions,
                    tenant_id=uuid.uuid4(),
                    request_session=session,
                )
            await session.close()
    finally:
        await engine.dispose()


async def test_connection_bound_caller_accepts_an_independent_owned_provider() -> None:
    """R1-001: connection-bound callers work when audit capacity is truly separate."""
    database_url = (
        f"sqlite+aiosqlite:///file:connection-bound-{uuid.uuid4()}"
        "?mode=memory&cache=shared&uri=true"
    )
    caller_engine = create_async_engine(database_url)
    audit_engine = create_async_engine(database_url)
    transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=1)
    try:
        async with caller_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        caller_factory = async_sessionmaker(caller_engine, expire_on_commit=False)
        async with caller_factory() as seed:
            tenant = await TenantRepository(seed).create(name="connection-bound")
            await seed.commit()

        async with caller_engine.connect() as connection:
            caller = AsyncSession(bind=connection)
            recorder = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant.id,
                request_session=caller,
            )
            event = await recorder.emit(
                actor=AuditActor.user(uuid.uuid4()),
                resource_type="assistant",
                resource_id=str(uuid.uuid4()),
                attempted_action="assistant.read",
                reason="not_visible",
                request_id="req-connection-bound",
                source_ip="unknown",
            )
            await caller.close()

        audit_factory = async_sessionmaker(audit_engine, expire_on_commit=False)
        async with audit_factory() as readback:
            events = await AuditEventRepository(readback, tenant.id).list_recent()
            assert [stored.id for stored in events] == [event.id]
    finally:
        await transactions.dispose()
        await caller_engine.dispose()


async def test_caller_engine_wrapper_sharing_audit_pool_is_rejected() -> None:
    """R1-001: distinct engine wrappers over one pool are still the same topology."""
    audit_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    caller_engine = audit_engine.execution_options(logging_token="request")
    try:
        async with AsyncSession(caller_engine) as caller:
            transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=1)
            with pytest.raises(UnsafeAuditTransactionTopology):
                PermissionDeniedRecorder(
                    transactions,
                    tenant_id=uuid.uuid4(),
                    request_session=caller,
                )
    finally:
        await audit_engine.dispose()


async def test_size_one_audit_pool_exhaustion_fails_closed_within_operation_bound() -> None:
    """R1-001: an occupied audit pool cannot hang a denial or fall back to caller SQL."""
    audit_engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
    )
    caller_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=0.1)
    try:
        async with audit_engine.connect():  # occupy the provider's only slot
            async with AsyncSession(caller_engine) as caller:
                recorder = PermissionDeniedRecorder(
                    transactions,
                    tenant_id=uuid.uuid4(),
                    request_session=caller,
                )
                started = monotonic()
                with pytest.raises(TimeoutError):
                    await recorder.emit(
                        actor=AuditActor.user(uuid.uuid4()),
                        resource_type="assistant",
                        resource_id=str(uuid.uuid4()),
                        attempted_action="assistant.read",
                        reason="not_visible",
                        request_id="req-audit-pool-exhausted",
                        source_ip="unknown",
                    )
                assert monotonic() - started < 1
    finally:
        await transactions.dispose()
        await caller_engine.dispose()


async def test_timed_out_sink_releases_size_one_audit_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operation deadline fails closed and does not leak its only connection."""
    temporary = TemporaryDirectory(prefix="lumen-audit579-poisoned-")
    audit_engine = create_async_engine(
        f"sqlite+aiosqlite:///{pathlib.Path(temporary.name) / 'poisoned.db'}",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    caller_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=0.1)
    invalidated: list[object] = []
    sa_event.listen(
        audit_engine.sync_engine.pool,
        "invalidate",
        lambda connection, record, error: invalidated.append((connection, record, error)),
    )
    try:
        async with audit_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        audit_factory = async_sessionmaker(audit_engine, expire_on_commit=False)
        async with audit_factory() as seed:
            tenant = await TenantRepository(seed).create(name="bounded audit")
            await seed.commit()

        async with AsyncSession(caller_engine) as caller:
            recorder = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant.id,
                request_session=caller,
            )
            original_record = AuditEventRepository.record

            async def _flush_then_never_return(
                repository: AuditEventRepository,
                *args: object,
                **kwargs: object,
            ) -> None:
                await original_record(repository, *args, **kwargs)  # type: ignore[arg-type]
                await asyncio.Event().wait()

            monkeypatch.setattr(AuditEventRepository, "record", _flush_then_never_return)
            started = monotonic()
            with pytest.raises(TimeoutError):
                await recorder.emit(
                    actor=AuditActor.user(uuid.uuid4()),
                    resource_type="assistant",
                    resource_id=str(uuid.uuid4()),
                    attempted_action="assistant.read",
                    reason="not_visible",
                    request_id="req-sink-timeout",
                    source_ip="unknown",
                )
            assert monotonic() - started < 1
            assert len(invalidated) == 2
            assert audit_engine.sync_engine.pool.checkedout() == 0

            # Immediate reuse proves timeout cleanup returned the only pool slot.
            monkeypatch.setattr(AuditEventRepository, "record", original_record)
            event = await recorder.emit(
                actor=AuditActor.user(uuid.uuid4()),
                resource_type="assistant",
                resource_id=str(uuid.uuid4()),
                attempted_action="assistant.read",
                reason="not_visible",
                request_id="req-after-timeout",
                source_ip="unknown",
            )

        async with audit_factory() as readback:
            events = await AuditEventRepository(readback, tenant.id).list_recent()
            assert [stored.id for stored in events] == [event.id]
    finally:
        await transactions.dispose()
        await caller_engine.dispose()
        temporary.cleanup()


async def test_transient_pre_persistence_timeout_retries_the_same_event_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-003: a bounded retry reuses one guard-assigned id and persists one row."""
    temporary = TemporaryDirectory(prefix="lumen-audit579-retry-")
    audit_engine = create_async_engine(
        f"sqlite+aiosqlite:///{pathlib.Path(temporary.name) / 'retry.db'}"
    )
    caller_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=0.05)
    try:
        async with audit_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        audit_factory = async_sessionmaker(audit_engine, expire_on_commit=False)
        async with audit_factory() as seed:
            tenant = await TenantRepository(seed).create(name="retry audit")
            await seed.commit()

        original_record = AuditEventRepository.record
        attempted_ids: list[uuid.UUID | None] = []

        async def _stall_once(
            repository: AuditEventRepository,
            *args: object,
            **kwargs: object,
        ) -> object:
            attempted_ids.append(kwargs.get("event_id"))  # type: ignore[arg-type]
            if len(attempted_ids) == 1:
                await asyncio.Event().wait()
            return await original_record(repository, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(AuditEventRepository, "record", _stall_once)
        async with AsyncSession(caller_engine) as caller:
            recorder = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant.id,
                request_session=caller,
            )
            event = await recorder.emit(
                actor=AuditActor.user(uuid.uuid4()),
                resource_type="assistant",
                resource_id=str(uuid.uuid4()),
                attempted_action="assistant.read",
                reason="not_visible",
                request_id="req-retry-same-id",
                source_ip="unknown",
            )

        # A fast retry invokes the operation twice.  On a loaded runner the
        # retry can persist and then lose its COMMIT acknowledgement, in which
        # case reconciliation invokes the same operation once more to validate
        # the canonical payload.  Both paths are bounded and must reuse the one
        # guard-assigned identity.
        assert 2 <= len(attempted_ids) <= 3
        assert set(attempted_ids) == {event.id}
        async with audit_factory() as readback:
            events = await AuditEventRepository(readback, tenant.id).list_recent()
            assert [stored.id for stored in events] == [event.id]
    finally:
        await transactions.dispose()
        await caller_engine.dispose()
        temporary.cleanup()


async def test_second_pre_persistence_timeout_fails_closed_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-003: a final absent reconciliation never turns two timeouts into success."""
    temporary = TemporaryDirectory(prefix="lumen-audit579-double-timeout-")
    audit_engine = create_async_engine(
        f"sqlite+aiosqlite:///{pathlib.Path(temporary.name) / 'double-timeout.db'}"
    )
    caller_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=0.05)
    try:
        async with audit_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        audit_factory = async_sessionmaker(audit_engine, expire_on_commit=False)
        async with audit_factory() as seed:
            tenant = await TenantRepository(seed).create(name="double timeout audit")
            await seed.commit()

        attempted_ids: list[uuid.UUID | None] = []

        async def _always_stall(
            repository: AuditEventRepository,
            *args: object,
            **kwargs: object,
        ) -> None:
            del repository, args
            attempted_ids.append(kwargs.get("event_id"))  # type: ignore[arg-type]
            await asyncio.Event().wait()

        monkeypatch.setattr(AuditEventRepository, "record", _always_stall)
        async with AsyncSession(caller_engine) as caller:
            recorder = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant.id,
                request_session=caller,
            )
            with pytest.raises(TimeoutError):
                await recorder.emit(
                    actor=AuditActor.user(uuid.uuid4()),
                    resource_type="assistant",
                    resource_id=str(uuid.uuid4()),
                    attempted_action="assistant.read",
                    reason="not_visible",
                    request_id="req-double-timeout",
                    source_ip="unknown",
                )

        assert len(attempted_ids) == 2
        assert attempted_ids[0] is not None
        assert attempted_ids[0] == attempted_ids[1]
        async with audit_factory() as readback:
            assert await AuditEventRepository(readback, tenant.id).list_recent() == []
    finally:
        await transactions.dispose()
        await caller_engine.dispose()
        temporary.cleanup()


async def test_post_commit_lost_ack_reconciles_exactly_once_and_leaves_no_commit_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2-003: committed-but-timed-out is success, never a duplicate or detached commit."""
    temporary = TemporaryDirectory(prefix="lumen-audit579-lost-ack-")
    audit_engine = create_async_engine(
        f"sqlite+aiosqlite:///{pathlib.Path(temporary.name) / 'lost-ack.db'}",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    caller_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=0.05)
    try:
        async with audit_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        audit_factory = async_sessionmaker(audit_engine, expire_on_commit=False)
        async with audit_factory() as seed:
            tenant = await TenantRepository(seed).create(name="lost ack audit")
            await seed.commit()

        real_commit = AsyncSession.commit
        real_invalidate = AsyncSession.invalidate
        committed = asyncio.Event()
        cancelled_after_commit = asyncio.Event()
        invalidated_sessions: list[AsyncSession] = []
        delayed_once = False

        async def _commit_then_lose_ack(audit_session: AsyncSession) -> None:
            nonlocal delayed_once
            await real_commit(audit_session)
            if audit_session.bind is audit_engine and not delayed_once:
                delayed_once = True
                committed.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled_after_commit.set()

        monkeypatch.setattr(AsyncSession, "commit", _commit_then_lose_ack)

        async def _track_invalidate(audit_session: AsyncSession) -> None:
            if audit_session.bind is audit_engine:
                invalidated_sessions.append(audit_session)
            await real_invalidate(audit_session)

        monkeypatch.setattr(AsyncSession, "invalidate", _track_invalidate)
        async with AsyncSession(caller_engine) as caller:
            recorder = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant.id,
                request_session=caller,
            )
            event = await recorder.emit(
                actor=AuditActor.user(uuid.uuid4()),
                resource_type="chat_session",
                resource_id=str(uuid.uuid4()),
                attempted_action="chat.session.read",
                reason="not_visible",
                request_id="req-post-commit-lost-ack",
                source_ip="203.0.113.90",
            )

        assert committed.is_set()
        assert cancelled_after_commit.is_set()
        assert len(invalidated_sessions) == 1
        # Reusing the sole slot immediately proves cleanup/reconciliation did not
        # strand a commit coroutine or the invalidated connection in the pool.
        async with AsyncSession(caller_engine) as caller:
            second = await PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant.id,
                request_session=caller,
            ).emit(
                actor=AuditActor.user(uuid.uuid4()),
                resource_type="chat_session",
                resource_id=str(uuid.uuid4()),
                attempted_action="chat.session.read",
                reason="not_visible",
                request_id="req-after-lost-ack",
                source_ip="203.0.113.90",
            )

        async with audit_factory() as readback:
            events = await AuditEventRepository(readback, tenant.id).list_recent()
            assert {stored.id for stored in events} == {event.id, second.id}
            assert sum(stored.id == event.id for stored in events) == 1
        assert audit_engine.sync_engine.pool.checkedout() == 0
    finally:
        await transactions.dispose()
        await caller_engine.dispose()
        temporary.cleanup()


async def test_explicit_audit_identity_is_concurrency_safe_for_equal_payloads() -> None:
    """R2-003: simultaneous same-key inserts converge on exactly one equal row."""
    temporary = TemporaryDirectory(prefix="lumen-audit579-concurrent-")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{pathlib.Path(temporary.name) / 'concurrent.db'}",
        connect_args={"timeout": 5},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="concurrent audit")
            await seed.commit()

        event_id = uuid.uuid4()
        actor_id = uuid.uuid4()
        resource_id = str(uuid.uuid4())

        async def _record() -> uuid.UUID:
            async with factory() as writer:
                event = await AuditEventRepository(writer, tenant.id).record(
                    event_id=event_id,
                    action=AuditAction.PERMISSION_DENIED.value,
                    resource_type="assistant",
                    outcome=AuditOutcome.DENIED,
                    actor_id=actor_id,
                    resource_id=resource_id,
                    request_id="req-concurrent-idempotency",
                    source_ip="203.0.113.91",
                    metadata={"attempted_action": "assistant.read", "reason": "not_visible"},
                )
                await writer.commit()
                return event.id

        assert await asyncio.gather(_record(), _record()) == [event_id, event_id]
        async with factory() as readback:
            events = await AuditEventRepository(readback, tenant.id).list_recent()
            assert [event.id for event in events] == [event_id]
    finally:
        await engine.dispose()
        temporary.cleanup()


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("actor_id", uuid.uuid4()),
        ("action", AuditAction.DOCUMENT_VIEWED.value),
        ("resource_type", "document"),
        ("resource_id", "assistant-different"),
        ("outcome", AuditOutcome.ALLOWED),
        ("request_id", "req-different"),
        ("source_ip", "system"),
        ("metadata", {"attempted_action": "assistant.update", "reason": "wrong_role"}),
    ],
)
async def test_explicit_audit_identity_rejects_conflicting_payload(
    changed_field: str,
    changed_value: object,
) -> None:
    """R2-003: every canonical field mismatch fails instead of blessing another event."""
    event_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    temporary = TemporaryDirectory(prefix="lumen-audit579-conflict-")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{pathlib.Path(temporary.name) / 'conflict.db'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="conflict audit")
            await session.commit()
            repository = AuditEventRepository(session, tenant.id)
            await repository.record(
                event_id=event_id,
                action=AuditAction.PERMISSION_DENIED.value,
                resource_type="assistant",
                outcome=AuditOutcome.DENIED,
                actor_id=actor_id,
                resource_id="assistant-original",
                request_id="req-original",
                source_ip="unknown",
                metadata={"attempted_action": "assistant.read", "reason": "not_visible"},
            )
            await session.commit()

            conflicting: dict[str, object] = {
                "action": AuditAction.PERMISSION_DENIED.value,
                "resource_type": "assistant",
                "outcome": AuditOutcome.DENIED,
                "actor_id": actor_id,
                "resource_id": "assistant-original",
                "request_id": "req-original",
                "source_ip": "unknown",
                "metadata": {"attempted_action": "assistant.read", "reason": "not_visible"},
            }
            conflicting[changed_field] = changed_value
            with pytest.raises(RuntimeError, match="idempotency.*payload"):
                await repository.record(
                    event_id=event_id,
                    **conflicting,  # type: ignore[arg-type]
                )

            events = await repository.list_recent()
            assert len(events) == 1
            assert events[0].id == event_id
            assert events[0].actor_id == actor_id
            assert events[0].resource_id == "assistant-original"
    finally:
        await engine.dispose()
        temporary.cleanup()


async def test_explicit_audit_identity_foreign_tenant_collision_fails_closed() -> None:
    """INV-1/R2-003: a hidden foreign row cannot satisfy local reconciliation."""
    temporary = TemporaryDirectory(prefix="lumen-audit579-tenant-collision-")
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{pathlib.Path(temporary.name) / 'tenant-collision.db'}"
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        event_id = uuid.uuid4()
        async with factory() as session:
            tenant_a = await TenantRepository(session).create(name="collision A")
            tenant_b = await TenantRepository(session).create(name="collision B")
            await session.commit()
            await AuditEventRepository(session, tenant_a.id).record(
                event_id=event_id,
                action=AuditAction.PERMISSION_DENIED.value,
                resource_type="assistant",
                outcome=AuditOutcome.DENIED,
                actor_id=None,
                resource_id="assistant-collision",
                request_id="req-collision",
                source_ip="system",
                metadata={"attempted_action": "assistant.run", "reason": "not_visible"},
            )
            await session.commit()

            repository_b = AuditEventRepository(session, tenant_b.id)
            assert await repository_b.get(event_id) is None
            with pytest.raises(RuntimeError, match="idempotency.*tenant"):
                await repository_b.record(
                    event_id=event_id,
                    action=AuditAction.PERMISSION_DENIED.value,
                    resource_type="assistant",
                    outcome=AuditOutcome.DENIED,
                    actor_id=None,
                    resource_id="assistant-collision",
                    request_id="req-collision",
                    source_ip="system",
                    metadata={"attempted_action": "assistant.run", "reason": "not_visible"},
                )
            assert await repository_b.list_recent() == []
    finally:
        await engine.dispose()
        temporary.cleanup()


async def test_generic_sink_repeated_payloads_keep_fresh_distinct_identities(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """R2-003 does not change ordinary non-denial append semantics."""
    sink = _sink(session, tenant_id)
    actor = AuditActor.user(uuid.uuid4())
    payload = {
        "action": AuditAction.DOCUMENT_VIEWED,
        "actor": actor,
        "resource_type": "document",
        "outcome": AuditOutcome.ALLOWED,
        "resource_id": str(uuid.uuid4()),
        "request_id": "req-generic-unchanged",
        "source_ip": "203.0.113.92",
    }

    first = await sink.emit(**payload)  # type: ignore[arg-type]
    second = await sink.emit(**payload)  # type: ignore[arg-type]

    assert first.id != second.id
    assert {event.id for event in await AuditEventRepository(session, tenant_id).list_recent()} == {
        first.id,
        second.id,
    }


async def test_durable_audit_provider_disposes_its_owned_engine() -> None:
    """The app-owned provider releases its dedicated pool during process shutdown."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    disposed: list[object] = []
    sa_event.listen(engine.sync_engine, "engine_disposed", lambda value: disposed.append(value))
    transactions = DurableAuditTransactions(engine, operation_timeout_seconds=1)

    await transactions.dispose()

    assert disposed == [engine.sync_engine]


async def test_durable_audit_commit_failure_leaves_no_partial_or_duplicate_event() -> None:
    """R1-001: a failed commit propagates; neither audit nor caller work survives."""
    audit_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    caller_engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    transactions = DurableAuditTransactions(audit_engine, operation_timeout_seconds=1)
    try:
        for engine in (audit_engine, caller_engine):
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        audit_factory = async_sessionmaker(audit_engine, expire_on_commit=False)
        caller_factory = async_sessionmaker(caller_engine, expire_on_commit=False)
        async with audit_factory() as seed:
            tenant = await TenantRepository(seed).create(name="audit tenant")
            await seed.commit()

        def _fail_commit(_connection: object) -> None:
            raise RuntimeError("forced audit commit failure")

        sa_event.listen(audit_engine.sync_engine, "commit", _fail_commit)
        async with caller_factory() as caller:
            pending = await TenantRepository(caller).create(name="caller must roll back")
            await caller.flush()
            recorder = PermissionDeniedRecorder(
                transactions,
                tenant_id=tenant.id,
                request_session=caller,
            )
            for ordinal in range(2):
                with pytest.raises(RuntimeError, match="forced audit commit failure"):
                    await recorder.emit(
                        actor=AuditActor.user(uuid.uuid4()),
                        resource_type="assistant",
                        resource_id=str(uuid.uuid4()),
                        attempted_action="assistant.read",
                        reason="not_visible",
                        request_id=f"req-commit-failure-{ordinal}",
                        source_ip="unknown",
                    )
            await caller.rollback()
            assert (
                await caller.execute(select(models.Tenant).where(models.Tenant.id == pending.id))
            ).scalar_one_or_none() is None

        async with audit_factory() as readback:
            assert await AuditEventRepository(readback, tenant.id).list_recent() == []
    finally:
        await transactions.dispose()
        await caller_engine.dispose()


async def test_emit_defaults_metadata_to_empty_dict(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    sink = _sink(session, tenant_id)
    event = await sink.emit(
        action=AuditAction.AUTH_LOGIN,
        actor=AuditActor.user(uuid.uuid4()),
        resource_type="session",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req-meta",
        source_ip="203.0.113.7",
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
            resource_id=str(uuid.uuid4()),
            request_id="req-1",
            source_ip="203.0.113.7",
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
            resource_id=str(uuid.uuid4()),
            request_id="req-2",
            source_ip="203.0.113.7",
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_missing_resource_id_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Spec 0004 §2.4: ``resource_id`` is required — a blank value persists nothing."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id="",  # required field missing
            request_id="req-3",
            source_ip="203.0.113.7",
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_missing_request_id_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Spec 0004 §2.4: ``request_id`` is required — a blank value persists nothing."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="   ",  # required field missing
            source_ip="203.0.113.7",
        )
    recent = await AuditEventRepository(session, tenant_id).list_recent()
    assert recent == []


async def test_inv6_missing_source_ip_is_rejected_before_write(
    session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    """Spec 0004 §2.4: ``source_ip`` is required — a blank value persists nothing."""
    sink = _sink(session, tenant_id)
    with pytest.raises(AuditEnvelopeError):
        await sink.emit(
            action=AuditAction.DOCUMENT_VIEWED,
            actor=AuditActor.user(uuid.uuid4()),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="req-4",
            source_ip="",  # required field missing
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


# --- the `source_ip` sentinel: why background audit writes used to roll back ---


async def test_a_background_sentinel_becomes_a_system_origin_not_an_inet_value(
    session: AsyncSession,
) -> None:
    """A background task's sentinel must not be handed to an ``INET`` column.

    The envelope requires a non-empty ``source_ip`` (spec 0004 §2.4), so a Celery task
    with no client address passes ``"system"``. The column is ``INET`` on Postgres,
    which rejects that — and because the emit rides the CALLER's transaction, the
    rejection rolled back the action being recorded. That is what killed the rolling
    summariser: `session_summaries` rows carried evidence (written in the answer
    transaction) but never a summary or a coverage cursor, and `session.summarized` was
    never written once.

    The fact the sentinel was carrying — "no client did this" — is not dropped. It
    moves to `source_origin`, where it is typed, queryable, and constrained.
    """
    tenant_id = uuid.uuid4()
    sink = AuditSink(AuditEventRepository(session, tenant_id))

    event = await sink.emit(
        action=AuditAction.SESSION_SUMMARIZED,
        actor=AuditActor.system(),
        resource_type="chat_session",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="celery-summarize",
        source_ip="system",
    )

    # Nothing that is not an address reaches the column…
    assert event.source_ip is None
    # …and the fact it stood for is recorded in its own right.
    assert event.source_origin == "system"


async def test_a_real_client_address_is_stored_unchanged(session: AsyncSession) -> None:
    """The request path must be unaffected — a real IP still lands in the column."""
    tenant_id = uuid.uuid4()
    sink = AuditSink(AuditEventRepository(session, tenant_id))

    for address in ("203.0.113.10", "2001:db8::1"):
        event = await sink.emit(
            action=AuditAction.RETRIEVAL_QUERY,
            actor=AuditActor.system(),
            resource_type="document",
            outcome=AuditOutcome.ALLOWED,
            resource_id=str(uuid.uuid4()),
            request_id="req-1",
            source_ip=address,
        )
        assert event.source_ip == address
        # A real client address means a `client` origin — and the repository writes
        # nothing into the caller's metadata to say so. Pinned because the interim fix
        # did exactly that, and re-adding it would resurrect the collision above.
        assert event.source_origin == "client"
        assert event.metadata == {}


async def test_the_sentinel_does_not_clobber_existing_metadata(session: AsyncSession) -> None:
    """Preserving the sentinel must not drop what the caller was already recording."""
    tenant_id = uuid.uuid4()
    sink = AuditSink(AuditEventRepository(session, tenant_id))

    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="chat_session",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="celery-1",
        source_ip="system",
        metadata={"covered_messages": 12},
    )

    assert event.metadata["covered_messages"] == 12
    assert event.source_origin == "system"


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        # Plain addresses, stored canonically.
        ("203.0.113.10", "203.0.113.10"),
        ("2001:db8::1", "2001:db8::1"),
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
        ("::ffff:1.2.3.4", "::ffff:102:304"),
        ("  192.168.1.5  ", "192.168.1.5"),
        # `INET` accepts CIDR too.
        ("10.0.0.0/8", "10.0.0.0/8"),
        ("10.1.2.3/8", "10.1.2.3/8"),
        # ZONE-SCOPED: Python parses it, Postgres does NOT — storing the raw text would
        # reproduce the very rollback this function exists to prevent, for any
        # link-local peer. The zone is dropped and the address kept.
        ("fe80::1%eth0", "fe80::1"),
        # BRACKETED / port-bearing: not an address to Python, so a naive parse would
        # silently NULL a REAL client address and lose audit fidelity.
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("[2001:db8::1]", "2001:db8::1"),
    ],
)
async def test_real_addresses_survive_in_every_form_a_client_can_present(
    session: AsyncSession, given: str, stored: str
) -> None:
    """The regression that would matter most: silently NULLing genuine client IPs."""
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip=given,
    )
    assert event.source_ip == stored
    assert event.source_origin == "client"


@pytest.mark.parametrize(
    ("given", "canonical"),
    [
        ("2001:0db8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
        ("::ffff:192.0.2.128", "::ffff:c000:280"),
        ("[2001:0db8:0:0:0:0:0:1]:443", "2001:db8::1"),
        ("fe80:0:0:0:0:0:0:1%eth0", "fe80::1"),
        ("10.1.2.3/8", "10.1.2.3/8"),
    ],
)
async def test_explicit_idempotency_uses_the_same_canonical_ip_on_insert_and_retry(
    session: AsyncSession,
    given: str,
    canonical: str,
) -> None:
    """R3-001: equivalent PostgreSQL INET spellings must reconcile as one payload."""
    tenant_id = uuid.uuid4()
    event_id = uuid.uuid4()
    repository = AuditEventRepository(session, tenant_id)
    kwargs = {
        "event_id": event_id,
        "action": AuditAction.PERMISSION_DENIED.value,
        "resource_type": "document",
        "outcome": AuditOutcome.DENIED,
        "actor_id": uuid.uuid4(),
        "resource_id": str(uuid.uuid4()),
        "request_id": "req-canonical-inet",
        "source_ip": given,
        "metadata": {"attempted_action": "document.read", "reason": "not_visible"},
    }

    first = await repository.record(**kwargs)
    retry_kwargs = {**kwargs, "source_ip": canonical}
    second = await repository.record(**retry_kwargs)

    assert first.id == second.id == event_id
    assert first.source_origin == second.source_origin == "client"
    assert first.source_ip == second.source_ip == canonical


async def test_a_caller_metadata_key_named_source_ip_is_not_clobbered(
    session: AsyncSession,
) -> None:
    """The collision that sank the interim fix.

    An audit event may legitimately carry an upstream `source_ip` of its own — a
    proxied peer, a webhook's caller — and that is a DIFFERENT fact from where the
    platform saw the request come from. Keeping the origin in `metadata` meant those
    two facts competed for one key. In its own column they cannot collide at all,
    and this test pins that: the caller's metadata comes back exactly as given.
    """
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip="system",
        metadata={"source_ip": "198.51.100.7"},
    )
    assert event.metadata["source_ip"] == "198.51.100.7"  # the caller's, untouched
    assert event.source_origin == "system"  # ours, in a column of its own
    assert event.source_ip is None


async def test_the_request_path_sentinel_is_handled_too(session: AsyncSession) -> None:
    """`request.client` is None for an AF_UNIX peer, so the app sends "unknown".

    Without this, a socket-mode proxy deployment would NULL `source_ip` on every event
    for genuine user traffic — silently, since the coercion never raises.
    """
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip="unknown",
    )
    # `unknown` is deliberately NOT `system`: a person did this and we could not see
    # from where, which is a different operational fact from "the platform did this".
    assert event.source_ip is None
    assert event.source_origin == "unknown"


async def test_an_unrecognised_non_address_is_recorded_as_unknown_not_system(
    session: AsyncSession,
) -> None:
    """A misconfigured proxy must not masquerade as background work.

    Coercing anything unparseable to `system` would hide a real operational problem
    behind a legitimate-looking origin — and would let a client action be filed as a
    platform action, which is exactly the confusion `source_origin` exists to remove.
    """
    sink = AuditSink(AuditEventRepository(session, uuid.uuid4()))
    event = await sink.emit(
        action=AuditAction.RETRIEVAL_QUERY,
        actor=AuditActor.system(),
        resource_type="document",
        outcome=AuditOutcome.ALLOWED,
        resource_id=str(uuid.uuid4()),
        request_id="req",
        source_ip="proxy-did-something-odd",
    )
    assert event.source_origin == "unknown"
    assert event.source_ip is None


async def test_the_database_refuses_a_pair_that_contradicts_itself(
    session: AsyncSession,
) -> None:
    """The origin/address agreement is a constraint, not a convention.

    The test above proves `record` no longer produces a contradictory pair — it did
    once, for every caller that passed no address at all. But `record` is not the only
    thing that will ever write this table: a later writer, a backfill, or a hand-run
    repair could. Pinning the rule in the schema means such a write fails loudly at the
    boundary instead of quietly filing a person's action as the platform's.
    """
    tenant_id = uuid.uuid4()
    for origin, ip in (("client", None), ("system", "203.0.113.10")):
        session.add(
            models.AuditEvent(
                tenant_id=tenant_id,
                actor_id=None,
                action=AuditAction.RETRIEVAL_QUERY.value,
                resource_type="document",
                resource_id=str(uuid.uuid4()),
                outcome=AuditOutcome.ALLOWED.value,
                request_id="req",
                source_origin=origin,
                source_ip=ip,
                event_metadata={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.parametrize(
    "absent",
    [None, "", "   "],
    ids=["none", "empty", "whitespace"],
)
async def test_no_address_at_all_is_unknown_and_not_a_contradictory_client(
    session: AsyncSession, absent: str | None
) -> None:
    """The regression that round 2 caught: absent is not the same as "a client".

    `record(source_ip=None)` is not hypothetical — `/auth/login`, `/auth/refresh` and
    `/auth/logout` call the repository DIRECTLY (bypassing the sink, and so bypassing
    the envelope's non-empty check), passing `request.client.host if request.client
    else ...`. `request.client` is None whenever uvicorn is bound to a UNIX socket
    behind nginx, which is the same AF_UNIX topology `unknown` was introduced for.

    The first version of this change inferred the origin from "no sentinel was
    returned", which absent input satisfies just as a real address does — so it wrote
    `client` with a NULL address, the one pair the CHECK constraint forbids. On a
    socket-bound deployment that would have aborted the login transaction the audit
    write was recording, and nobody could sign in: the exact failure this whole change
    exists to remove, relocated from Celery to the front door.

    No existing test could catch it, because httpx's ASGI transport always populates
    `scope["client"]` — so the suite never once exercised the branch production takes.
    """
    # Deliberately NOT through `AuditSink.emit`: the envelope refuses an empty
    # `source_ip`, so the sink would mask this. `AuthService` calls the repository
    # directly — that is the unguarded path production actually takes, and the reason
    # this survived a round of review.
    repo = AuditEventRepository(session, (await TenantRepository(session).create(name="Acme")).id)
    event = await repo.record(
        actor_id=None,
        action=AuditAction.AUTH_LOGIN.value,
        resource_type="session",
        resource_id=str(uuid.uuid4()),
        outcome=AuditOutcome.ALLOWED,
        request_id="req",
        source_ip=absent,
    )
    # `unknown`, because something asked for this — we just could not see from where.
    assert event.source_origin == "unknown"
    assert event.source_ip is None
    # And it actually reaches the table: the constraint is satisfied, not merely dodged.
    await session.flush()


async def test_the_auth_routes_no_longer_send_a_bare_none(session: AsyncSession) -> None:
    """Defence in depth: fix the writer AND stop the outlier caller.

    `record` now handles a missing address correctly, so this is belt-and-braces — but
    `auth.py` was the ONLY router passing `None` where the other fourteen pass
    `"unknown"`, and that inconsistency is what routed production down an untested
    branch. Pinned as source text because the alternative (spinning up a socket-bound
    server) tests the transport rather than the decision.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "auth.py"
    ).read_text(encoding="utf-8")
    assert "request.client else None" not in source
    assert source.count('request.client else "unknown"') == 3
