"""Every sandbox exit hands the caller a tenant-bound transaction (#838).

On PostgreSQL the RLS tenant GUC is transaction-local (``bind_tenant`` is
``set_config(..., true)``), so ``commit``/``rollback`` silently unbinds the
session and the next tenant-scoped query fails under a NOBYPASSRLS role
(``invalid input syntax for type uuid: ""``). ``bind_tenant`` is a no-op on the
SQLite these tests run on, and the compose role bypasses RLS, so the
accounting-loss KILLED exit shipped unbound. This module emulates the Postgres
semantics: ending the ROOT transaction (commit or rollback) clears a per-session
flag, the sandbox modules' ``bind_tenant`` sets it, and a ``code_runs`` query on
an unbound session fails. Releasing a SAVEPOINT does not end the transaction, so
it keeps ``SET LOCAL`` on PostgreSQL and keeps the flag here.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session, SessionTransaction

from app.db import models
from app.domain.code_execution import SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
from app.domain.entities import CodeRunStatus
from app.realtime.backplane import InMemoryBackplane
from app.sandbox.accounting import AccountingUnavailable, InMemorySandboxAccounting
from app.sandbox.runner import SandboxRunnerUnavailable
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec
from tests.test_sandbox_tool_runner import _BarrierSleep, _FakeRunner, _ok_result, _World
from tests.test_sandbox_tool_runner import world as world  # noqa: F401 - fixture

_BOUND = "emulated_tenant_guc"


@pytest.fixture
def transaction_scoped_binding(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Postgres GUC semantics on SQLite; yields the unbound code_runs queries seen."""
    violations: list[str] = []

    async def bind_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
        session.sync_session.info[_BOUND] = tenant_id

    def unbind(session: Session, transaction: SessionTransaction) -> None:
        if transaction.parent is None:
            session.info.pop(_BOUND, None)

    def guard(state: ORMExecuteState) -> None:
        if _BOUND in state.session.info:
            return
        if any(mapper.class_ is models.CodeRun for mapper in state.all_mappers):
            violations.append(str(state.statement).split("\n", 1)[0])
            raise AssertionError("tenant-scoped code_runs query on an unbound transaction")

    monkeypatch.setattr("app.sandbox.service.bind_tenant", bind_tenant)
    monkeypatch.setattr("app.sandbox.tool_runner.bind_tenant", bind_tenant)
    hooks = (("after_transaction_end", unbind), ("do_orm_execute", guard))
    for name, fn in hooks:
        event.listen(Session, name, fn)
    try:
        yield violations
    finally:
        for name, fn in hooks:
            event.remove(Session, name, fn)


async def test_success_exit_is_tenant_bound(
    world: _World,  # noqa: F811
    transaction_scoped_binding: list[str],
) -> None:
    seam = world.seam(_FakeRunner(result=_ok_result()), backplane=InMemoryBackplane())
    summary = await seam.submit(code="print('hello')")
    assert summary.status is CodeRunStatus.SUCCEEDED
    assert transaction_scoped_binding == []


async def test_runner_failure_exit_is_tenant_bound(
    world: _World,  # noqa: F811
    transaction_scoped_binding: list[str],
) -> None:
    runner = _FakeRunner(raises=SandboxRunnerUnavailable("runner down"))
    seam = world.seam(runner, backplane=InMemoryBackplane())
    summary = await seam.submit(code="print('hello')")
    assert summary.status is CodeRunStatus.FAILED
    assert transaction_scoped_binding == []


async def test_accounting_loss_killed_exit_is_tenant_bound(
    world: _World,  # noqa: F811
    transaction_scoped_binding: list[str],
) -> None:
    """The kill loop commits the KILLED row; the caller then re-reads it."""
    entered = asyncio.Event()
    heartbeat = _BarrierSleep()

    class Accounting(InMemorySandboxAccounting):
        async def renew_lease(self, *args: Any, **kwargs: Any) -> None:
            raise AccountingUnavailable("accounting down")

    class Runner(_FakeRunner):
        async def execute(self, value: SandboxSessionSpec, spec: RunSpec) -> RunResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    async def no_retry(_delay: float) -> None:
        # The kill is confirmed on the first attempt; a retry means its bookkeeping
        # failed, and retrying on real sleeps would only hang the test.
        raise AssertionError("the confirmed kill was retried")

    seam = world.seam(
        Runner(),
        backplane=InMemoryBackplane(),
        accounting=Accounting(),
        lease_sleep=heartbeat,
        retry_sleep=no_retry,
    )
    task = asyncio.create_task(seam.submit(code="while True: pass"))
    try:
        await entered.wait()
        (await heartbeat.waits.get()).set_result(None)
        summary = await asyncio.wait_for(task, 10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert summary.status is CodeRunStatus.KILLED
    assert summary.reason_code == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
    assert transaction_scoped_binding == []
