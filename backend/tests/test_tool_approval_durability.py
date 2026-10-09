"""#518 / R1-002: approval intent must survive the answer transaction.

Real registered run_python → chat submission seam → sandbox service. Only the
external runner/storage are faked. File SQLite gives genuinely separate transactions;
the same cases can run on the isolated PostgreSQL database under a restricted role.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.principal import Principal
from app.db import models
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    TenantToolPolicyRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditActor
from app.domain.entities import Role
from app.domain.llm import ToolCall
from app.realtime.backplane import InMemoryBackplane
from app.services.audit import AuditSink
from app.services.tools.gate import PolicyApprovalGate
from app.services.tools.runner import ToolRunner, hash_args
from app.services.tools.types import ToolContext
from tests._db_helpers import copy_sqlite_schema
from tests._live_helpers import isolated_live_url, worker_database_name
from tests.test_sandbox_tool_runner import _FakeRunner, _ok_result, _World


@pytest_asyncio.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.live,
                pytest.mark.skipif(
                    os.environ.get("RUN_PR559_LIVE") != "1", reason="isolated PR559 live opt-in"
                ),
            ],
        ),
    ]
)
async def world(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[_World]:
    if request.param == "postgres":
        url = os.environ["DATABASE_URL"]
        assert url == isolated_live_url(
            "postgresql+asyncpg://lumen:lumen_local_dev@localhost:47182/lumentest_pr559"
        )
        engine = create_async_engine(
            url,
            connect_args={"server_settings": {"role": worker_database_name("lumentest_pr559_r2")}},
        )
    else:
        url = f"sqlite+aiosqlite:///{(tmp_path / 'approval.db').as_posix()}"
        engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        if request.param == "sqlite":
            async with engine.begin() as conn:
                await conn.run_sync(copy_sqlite_schema)
        async with factory() as session:
            if request.param == "postgres":
                flags = (
                    await session.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
                        )
                    )
                ).one()
                assert flags == (False, False)
            tenant = await TenantRepository(session).create(name="PR559 durability")
            await bind_tenant(session, tenant.id)
            user = await UserRepository(session, tenant.id).create(
                email=f"pr559-{uuid.uuid4()}@test.invalid", password_hash="x", roles=[Role.ADMIN]
            )
            await TenantSandboxPolicyRepository(session, tenant.id).upsert(
                enabled=True,
                allowed_packages=(),
                denied_packages=(),
                egress_allowed=False,
                egress_allowlist=(),
                max_runtime_s=30,
                max_memory_mb=512,
                daily_runtime_cap_s=3600,
                max_concurrency=2,
                updated_by=user.id,
            )
            await TenantToolPolicyRepository(session, tenant.id).upsert(
                tool_name="run_python",
                enabled=True,
                requires_approval=False,
                updated_by=user.id,
            )
            chat = await ChatSessionRepository(session, tenant.id).create(
                owner_id=user.id, model="m", title="chat"
            )
            await session.commit()
            await bind_tenant(session, tenant.id)
            yield _World(
                session=session,
                sessionmaker=factory,
                tenant_id=tenant.id,
                user_id=user.id,
                chat_id=chat.id,
            )
    finally:
        await engine.dispose()


def _runner(world: _World) -> ToolRunner:
    return ToolRunner(
        allowed=frozenset({"run_python"}),
        invocations=ToolInvocationRepository(world.session, world.tenant_id),
        audit=AuditSink(AuditEventRepository(world.session, world.tenant_id)),
        actor=AuditActor.user(world.user_id),
        request_id="pr559-durable",
        source_ip="127.0.0.1",
        session_id=world.chat_id,
        gate=PolicyApprovalGate(world.session, tenant_id=world.tenant_id),
    )


def _context(world: _World, external: _FakeRunner) -> ToolContext:
    return ToolContext(
        principal=Principal(user_id=world.user_id, tenant_id=world.tenant_id, roles=(Role.ADMIN,)),
        retrieval=object(),  # type: ignore[arg-type]
        sandbox=world.seam(external, backplane=InMemoryBackplane()),
        session_id=world.chat_id,
    )


CALL = ToolCall(id="durable-c1", name="run_python", arguments={"code": "print(1)"})


async def test_chat_cancel_preserves_approval_before_external_dispatch(world: _World) -> None:
    dispatched = asyncio.Event()
    release = asyncio.Event()

    class BlockingRunner(_FakeRunner):
        async def execute(self, value, spec):
            self.calls += 1
            dispatched.set()
            await release.wait()
            return _ok_result()

    external = BlockingRunner()
    # A pending answer-side write must not be committed by the audit transaction.
    pending_id = uuid.uuid4()
    world.session.add(
        models.ChatSession(
            id=pending_id,
            tenant_id=world.tenant_id,
            owner_id=world.user_id,
            model="m",
            title="pending",
        )
    )
    if world.session.bind.dialect.name == "postgresql":
        await world.session.flush()  # prove an actual pending DML transaction stays uncommitted
    task = asyncio.create_task(_runner(world).run(call=CALL, context=_context(world, external)))
    dispatch_waiter = asyncio.create_task(dispatched.wait())
    try:
        done, _ = await asyncio.wait({task, dispatch_waiter}, return_when=asyncio.FIRST_COMPLETED)
        if dispatch_waiter not in done:
            await task  # surface an early infrastructure failure instead of hanging
        # Read from a fresh transaction WHILE execution is suspended. Persistence
        # on cancellation or after the handler completes is too late.
        async with world.sessionmaker() as reader:
            await bind_tenant(reader, world.tenant_id)
            events = await AuditEventRepository(reader, world.tenant_id).list_recent(limit=30)
        invoked = [e for e in events if e.action == "tool.invoked"]
        assert len(invoked) == 1, "external execution started without committed approval evidence"
    finally:
        dispatch_waiter.cancel()
        await asyncio.gather(dispatch_waiter, return_exceptions=True)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await world.session.rollback()

    await bind_tenant(world.session, world.tenant_id)
    assert await ChatSessionRepository(world.session, world.tenant_id).get(pending_id) is None
    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=30)
    invoked = [e for e in events if e.action == "tool.invoked"]
    assert len(invoked) == 1
    assert invoked[0].metadata["approved_by"] == str(world.user_id)
    assert invoked[0].metadata["approval_policy_id"]
    assert invoked[0].metadata["approval_scope"] == "tenant_preapproval"
    assert invoked[0].metadata["args_hash"] == hash_args(CALL.arguments)
    assert invoked[0].metadata["session_id"] == str(world.chat_id)
    assert not any(e.action == "tool.result" for e in events)  # no fabricated outcome
    runs = (
        await world.session.execute(
            text("SELECT status, code FROM code_runs WHERE tenant_id=:tenant"),
            {
                "tenant": world.tenant_id.hex
                if world.session.bind.dialect.name == "sqlite"
                else world.tenant_id
            },
        )
    ).all()
    assert runs == [("running", "print(1)")]
    assert external.calls == 1


async def test_completed_chat_result_correlates_to_durable_intent_after_rollback(
    world: _World,
) -> None:
    external = _FakeRunner(result=_ok_result())
    result = await _runner(world).run(call=CALL, context=_context(world, external))
    assert result.ok
    await world.session.rollback()
    await bind_tenant(world.session, world.tenant_id)
    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=30)
    invoked = [e for e in events if e.action == "tool.invoked"]
    outcomes = [e for e in events if e.action == "tool.result"]
    assert len(invoked) == len(outcomes) == 1
    assert outcomes[0].metadata["invoked_event_id"] == str(invoked[0].id)
    assert outcomes[0].metadata["args_hash"] == hash_args(CALL.arguments)


async def test_failed_approval_intent_write_never_dispatches(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = AuditEventRepository.record

    async def fail_intent(self, **kwargs):
        if kwargs["action"] == "tool.invoked":
            raise RuntimeError("audit unavailable")
        return await original(self, **kwargs)

    monkeypatch.setattr(AuditEventRepository, "record", fail_intent)
    external = _FakeRunner(result=_ok_result())
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await _runner(world).run(call=CALL, context=_context(world, external))
    assert external.calls == 0
