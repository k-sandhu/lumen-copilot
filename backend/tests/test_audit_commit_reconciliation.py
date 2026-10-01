"""R4-001: ambiguous COMMIT errors reconcile the original denial identity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.db.audit_transactions import DurableAuditTransactions
from app.db.base import Base
from app.db.repositories import AuditEventRepository, TenantRepository
from app.domain.audit import AuditAction
from app.domain.entities import AuditOutcome


@pytest.mark.parametrize(
    "failure", ["lost_ack", "disconnect", "cancel", "timeout", "precommit", "mismatch"]
)
async def test_ambiguous_commit_reconciles_same_identity_and_releases_capacity(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
    )
    caller_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'caller.db'}")
    provider = DurableAuditTransactions(engine, operation_timeout_seconds=10)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_commit = AsyncSession.commit
    event_id, actor_id = uuid4(), uuid4()
    attempted_ids = []
    commits = 0
    try:
        for db in (engine, caller_engine):
            async with db.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        async with factory() as seed:
            tenant = await TenantRepository(seed).create(name="R4-001")
            await seed.commit()

        async def commit(session: AsyncSession) -> None:
            nonlocal commits
            commits += 1
            if commits == 1:
                if failure != "disconnect":
                    await real_commit(session)
                entered.set()
                await release.wait()
                if failure in {"disconnect", "lost_ack", "mismatch"}:
                    raise DBAPIError(
                        "COMMIT", {}, OSError("connection lost"), connection_invalidated=True
                    )
                if failure == "timeout":
                    raise TimeoutError("lost acknowledgement")
            else:
                await real_commit(session)

        async def operation(repository: AuditEventRepository):
            attempted_ids.append(event_id)
            if failure == "precommit":
                raise DBAPIError(
                    "INSERT", {}, OSError("connection lost"), connection_invalidated=True
                )
            return await repository.record(
                event_id=event_id,
                actor_id=actor_id,
                action=AuditAction.PERMISSION_DENIED.value,
                resource_type="assistant",
                resource_id="hidden",
                outcome=AuditOutcome.DENIED,
                request_id="R4-001",
                source_ip="unknown",
                metadata={
                    "attempted_action": "assistant.update"
                    if failure == "mismatch" and len(attempted_ids) > 1
                    else "assistant.read",
                    "reason": "not_visible",
                },
            )

        monkeypatch.setattr(AsyncSession, "commit", commit)
        async with AsyncSession(caller_engine) as caller:
            pending = await TenantRepository(caller).create(name="must roll back")
            provider.assert_independent_from(caller)
            task = asyncio.create_task(provider.execute_idempotent(tenant.id, event_id, operation))
            if failure == "precommit":
                with pytest.raises(DBAPIError):
                    await task
            else:
                await entered.wait()
                if failure == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    release.set()
                    if failure == "mismatch":
                        with pytest.raises(RuntimeError, match="canonical payload"):
                            await task
                    else:
                        result = await task
                        assert result.id == event_id
            await caller.rollback()
            assert await TenantRepository(caller).get(pending.id) is None

        assert set(attempted_ids) == {event_id}
        assert engine.sync_engine.pool.checkedout() == 0
        async with factory() as readback:
            rows = await AuditEventRepository(readback, tenant.id).list_recent()
            assert [row.id for row in rows] == ([] if failure == "precommit" else [event_id])
        assert commits == (0 if failure == "precommit" else 2 if failure == "disconnect" else 1)
        assert len(attempted_ids) == (1 if failure == "precommit" else 2)
    finally:
        release.set()
        await provider.dispose()
        await caller_engine.dispose()
