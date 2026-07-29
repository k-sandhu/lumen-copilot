"""Every blocked ``run_python`` names the gate that refused it (#502) — and #500.

Four INDEPENDENT switches can refuse a code run, and until #502 all four produced
the same opaque "code execution denied" reply, so an operator could not tell which
one to flip. This module pins the diagnostic contract: each refusal carries a
DISTINCT, typed, actionable reason that reaches

* (i) the tool result the model reads (``content``),
* (ii) the ``tool_invocations`` row / the audit trail (``result_summary`` /
  ``reason_code`` metadata),
* (iii) a structured log (asserted at the audit/reason-code level here).

The four gates:

1. the **tenant tool policy** (``services/tools/gate.py``) — absent row, disabled,
   or ``requires_approval`` (issue #500: approval is *unavailable* in chat, which
   must read differently from "disabled"), or unreadable (fail-closed);
2. the **deploy kill-switch** ``SANDBOX_ENABLED`` (``core/config.py``);
3. the **tenant sandbox policy** row (``services/sandbox_policy_service.py``) —
   absent or ``enabled=false``;
4. the **runner being unreachable** (``sandbox/runner.py`` → ``DependencyError``).

The established error CODES are unchanged (``approval_denied`` /
``code_execution_denied``); the reason is ADDITIVE specificity on top of them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.core.errors import DependencyError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    CodeRunRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    TenantToolPolicyRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.domain.audit import AuditActor
from app.domain.code_execution import (
    SANDBOX_REASON_DEPLOY_DISABLED,
    SANDBOX_REASON_POLICY_ABSENT,
    SANDBOX_REASON_RUNNER_UNAVAILABLE,
    SANDBOX_REASON_TENANT_DISABLED,
)
from app.domain.entities import CodeRunStatus, ResourceUsage, Role
from app.domain.llm import ToolCall
from app.domain.tools import (
    APPROVAL_REASON_APPROVAL_UNAVAILABLE,
    APPROVAL_REASON_POLICY_ABSENT,
    APPROVAL_REASON_POLICY_DISABLED,
    APPROVAL_REASON_POLICY_UNREADABLE,
    ERROR_APPROVAL_DENIED,
    RiskTier,
    ToolHandlerResult,
)
from app.sandbox.service import SandboxService
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec
from app.services.audit import AuditSink
from app.services.tools.gate import PolicyApprovalGate
from app.services.tools.runner import ToolRunner
from app.services.tools.types import ApprovalRequest, ToolContext, ToolDefinition
from tests._sandbox_helpers import sandbox_settings

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


# ---------------------------------------------------------------------------
# World
# ---------------------------------------------------------------------------


class _World:
    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: UUID,
        user_id: UUID,
        chat_id: UUID,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.chat_id = chat_id

    @property
    def principal(self) -> Principal:
        return Principal(user_id=self.user_id, tenant_id=self.tenant_id, roles=(Role.MEMBER,))


@pytest_asyncio.fixture
async def world() -> AsyncIterator[_World]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            tenant = await TenantRepository(session).create(name="Acme")
            user = await UserRepository(session, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            chat = await ChatSessionRepository(session, tenant.id).create(
                owner_id=user.id, model="openrouter:test/model"
            )
            await session.commit()
            yield _World(
                session=session,
                tenant_id=tenant.id,
                user_id=user.id,
                chat_id=chat.id,
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Gate 1 — the tenant TOOL policy (services/tools/gate.py)
# ---------------------------------------------------------------------------


def _approval_request(world: _World) -> ApprovalRequest:
    return ApprovalRequest(
        call_id="call-1",
        tool_name="run_python",
        risk_tier=RiskTier.T2,
        principal=world.principal,
        arguments={"code": "print(1)"},
    )


async def test_absent_tool_policy_names_the_tool_policy_switch(world: _World) -> None:
    """No admin row ⇒ denied *because no policy enables it* — not "approval denied"."""
    decision = await PolicyApprovalGate(world.session, tenant_id=world.tenant_id).request(
        _approval_request(world)
    )
    assert decision.approved is False
    assert decision.reason == APPROVAL_REASON_POLICY_ABSENT
    assert decision.detail is not None
    # Actionable: it names the switch an admin has to flip.
    assert "tool governance" in decision.detail.lower()


async def test_disabled_tool_policy_names_the_tool_policy_switch(world: _World) -> None:
    """An explicitly disabled tool is its own reason, distinct from an absent row."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=False, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()
    decision = await PolicyApprovalGate(world.session, tenant_id=world.tenant_id).request(
        _approval_request(world)
    )
    assert decision.approved is False
    assert decision.reason == APPROVAL_REASON_POLICY_DISABLED


async def test_requires_approval_says_approval_is_unavailable_not_disabled(
    world: _World,
) -> None:
    """#500 — the honesty fix: "requires approval" is a PERMANENT deny in chat.

    There is no interactive approver on the chat surface, so a tool an admin
    enabled but left marked "requires approval" is refused every time. That must
    read as *approval unavailable here*, distinguishable from "disabled", or an
    admin cannot tell that ticking the box silently switched the tool off.
    """
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()
    decision = await PolicyApprovalGate(world.session, tenant_id=world.tenant_id).request(
        _approval_request(world)
    )
    assert decision.approved is False
    assert decision.reason == APPROVAL_REASON_APPROVAL_UNAVAILABLE
    assert decision.reason != APPROVAL_REASON_POLICY_DISABLED
    assert decision.detail is not None
    detail = decision.detail.lower()
    # It must state that approval is required AND that no approver exists here.
    assert "approval" in detail
    assert "no approver" in detail or "cannot be approved" in detail
    # And name the switch that unblocks it.
    assert "requires approval" in detail


async def test_unreadable_tool_policy_is_its_own_reason(world: _World) -> None:
    """Fail-closed (INV-7) — but say so, rather than looking like "not enabled"."""

    class _BoomSession:
        async def execute(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("db is down")

    decision = await PolicyApprovalGate(
        _BoomSession(),  # type: ignore[arg-type]
        tenant_id=world.tenant_id,
    ).request(_approval_request(world))
    assert decision.approved is False
    assert decision.reason == APPROVAL_REASON_POLICY_UNREADABLE


def _gated_tool(calls: list[str]) -> ToolDefinition:
    async def _handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        calls.append("ran")
        return ToolHandlerResult(content="did the thing", summary="ok")

    return ToolDefinition(
        name="run_python",
        description="a gated tool",
        json_schema={"type": "object", "properties": {}},
        handler=_handler,
        risk_tier=RiskTier.T2,
        requires_approval=True,
        read_only=False,
    )


async def test_approval_reason_reaches_model_trace_and_audit(
    world: _World, monkeypatch: Any
) -> None:
    """The reason reaches (i) the model, (ii) the trace row, (iii) the audit metadata."""
    calls: list[str] = []
    monkeypatch.setattr("app.services.tools.runner.get_tool", lambda name: _gated_tool(calls))
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()

    session_id = uuid.uuid4()
    runner = ToolRunner(
        allowed=frozenset({"run_python"}),
        invocations=ToolInvocationRepository(world.session, world.tenant_id),
        audit=AuditSink(AuditEventRepository(world.session, world.tenant_id)),
        actor=AuditActor.user(world.user_id),
        request_id="req-1",
        source_ip="203.0.113.7",
        session_id=session_id,
        gate=PolicyApprovalGate(world.session, tenant_id=world.tenant_id),
    )
    result = await runner.run(
        call=ToolCall(id="c1", name="run_python", arguments={"code": "print(1)"}),
        context=ToolContext(principal=world.principal, retrieval=object()),  # type: ignore[arg-type]
    )
    await world.session.commit()

    # The established code is unchanged — the reason is ADDITIVE.
    assert result.ok is False
    assert result.error == ERROR_APPROVAL_DENIED
    assert calls == []
    # (i) the model sees the specific reason, not a generic "not granted".
    assert "requires approval" in result.content.lower()
    # (ii) the tool_invocations row carries the typed reason.
    rows = await ToolInvocationRepository(world.session, world.tenant_id).list_for_session(
        session_id
    )
    assert any(
        r.result_summary is not None and APPROVAL_REASON_APPROVAL_UNAVAILABLE in r.result_summary
        for r in rows
    )
    # (iii) the audit metadata carries it too.
    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=20)
    assert any(
        e.metadata.get("denied_reason") == APPROVAL_REASON_APPROVAL_UNAVAILABLE for e in events
    )


# ---------------------------------------------------------------------------
# Gates 2-4 — the sandbox admission path
# ---------------------------------------------------------------------------


class _FakeStore:
    async def put_artifact(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> object:  # pragma: no cover — no run in this module produces output
        raise AssertionError("no refused run may produce artifacts")


class _FakeRunner:
    def __init__(self, *, ensure_raises: Exception | None = None) -> None:
        self.ensure_raises = ensure_raises
        self.ensured: list[SandboxSessionSpec] = []
        self.executions: list[tuple[SandboxSessionSpec, RunSpec]] = []

    async def ensure_session(self, value: SandboxSessionSpec) -> None:
        self.ensured.append(value)
        if self.ensure_raises is not None:
            raise self.ensure_raises

    async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
        self.executions.append((value, run))
        return RunResult(
            status=CodeRunStatus.SUCCEEDED,
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=1,
            resource_usage=ResourceUsage(),
        )

    async def reset_session(
        self, previous: SandboxSessionSpec, replacement: SandboxSessionSpec
    ) -> None: ...

    async def close_session(self, value: SandboxSessionSpec) -> None: ...

    async def cancel(self, value: SandboxSessionSpec, execution_id: UUID) -> None: ...


async def _enable_tenant_sandbox(world: _World, *, enabled: bool = True) -> None:
    await TenantSandboxPolicyRepository(world.session, world.tenant_id).upsert(
        enabled=enabled,
        allowed_packages=(),
        denied_packages=(),
        egress_allowed=False,
        egress_allowlist=(),
        max_runtime_s=30,
        max_memory_mb=512,
        daily_runtime_cap_s=3600,
        max_concurrency=2,
        updated_by=None,
    )


async def _refuse(
    world: _World,
    *,
    deploy_enabled: bool = True,
    runner: _FakeRunner | None = None,
) -> tuple[CodeRunStatus, str, dict[str, object]]:
    """Run one code run and return (status, stderr, the terminal audit metadata)."""
    run = await CodeRunRepository(world.session, world.tenant_id).create(
        owner_id=world.user_id,
        session_id=world.chat_id,
        code="print(1)",
        requested_packages=(),
    )
    service = SandboxService(
        tenant_id=world.tenant_id,
        owner_id=world.user_id,
        runner=runner or _FakeRunner(),  # type: ignore[arg-type]
        object_store=_FakeStore(),  # type: ignore[arg-type]
        settings=sandbox_settings(SANDBOX_ENABLED="true" if deploy_enabled else "false"),
    )
    status = await service.execute(world.session, run.id)
    persisted = await CodeRunRepository(world.session, world.tenant_id).get(run.id)
    assert persisted is not None
    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=20)
    terminal = next(e for e in events if e.action in {"code_run.denied", "code_run.finished"})
    return status, persisted.stderr, dict(terminal.metadata)


async def test_deploy_kill_switch_names_sandbox_enabled(world: _World) -> None:
    """Gate 2 — the tenant is fully enabled; only the DEPLOY switch is off."""
    await _enable_tenant_sandbox(world)
    await world.session.commit()
    status, stderr, metadata = await _refuse(world, deploy_enabled=False)

    assert status is CodeRunStatus.DENIED
    assert metadata["reason_code"] == SANDBOX_REASON_DEPLOY_DISABLED
    # Names the exact switch an operator flips (a deploy env var, not an admin toggle).
    assert "SANDBOX_ENABLED" in stderr


async def test_absent_tenant_sandbox_policy_names_the_workspace_policy(
    world: _World,
) -> None:
    """Gate 3a — deploy switch on, but the workspace has no sandbox policy row."""
    status, stderr, metadata = await _refuse(world)

    assert status is CodeRunStatus.DENIED
    assert metadata["reason_code"] == SANDBOX_REASON_POLICY_ABSENT
    assert "SANDBOX_ENABLED" not in stderr  # NOT the deploy switch's fault
    assert "sandbox" in stderr.lower()


async def test_disabled_tenant_sandbox_policy_is_distinct_from_an_absent_one(
    world: _World,
) -> None:
    """Gate 3b — a stored row with ``enabled=false`` is its own reason."""
    await _enable_tenant_sandbox(world, enabled=False)
    await world.session.commit()
    status, _, metadata = await _refuse(world)

    assert status is CodeRunStatus.DENIED
    assert metadata["reason_code"] == SANDBOX_REASON_TENANT_DISABLED


async def test_unreachable_runner_names_the_runner_not_a_policy(world: _World) -> None:
    """Gate 4 — every policy says yes; the runner service is simply not there."""
    await _enable_tenant_sandbox(world)
    await world.session.commit()
    runner = _FakeRunner(
        ensure_raises=DependencyError(
            "sandbox runner unavailable", code="sandbox_runner_unavailable"
        )
    )
    status, stderr, metadata = await _refuse(world, runner=runner)

    # An unreachable dependency is a FAILED run (not a policy denial) — but it must
    # say WHICH dependency, so nobody hunts for a policy switch that is already on.
    assert status is CodeRunStatus.FAILED
    assert metadata["reason_code"] == SANDBOX_REASON_RUNNER_UNAVAILABLE
    assert "unreachable" in stderr.lower()


async def test_the_four_gates_give_pairwise_distinct_reasons(world: _World) -> None:
    """The whole point of #502: four gates, four distinguishable answers."""
    reasons = {
        APPROVAL_REASON_APPROVAL_UNAVAILABLE,
        SANDBOX_REASON_DEPLOY_DISABLED,
        SANDBOX_REASON_TENANT_DISABLED,
        SANDBOX_REASON_RUNNER_UNAVAILABLE,
    }
    assert len(reasons) == 4
    # …and the tool-policy family is itself split into its four sub-reasons.
    tool_policy = {
        APPROVAL_REASON_POLICY_ABSENT,
        APPROVAL_REASON_POLICY_DISABLED,
        APPROVAL_REASON_APPROVAL_UNAVAILABLE,
        APPROVAL_REASON_POLICY_UNREADABLE,
    }
    assert len(tool_policy) == 4
    assert reasons.isdisjoint(tool_policy - {APPROVAL_REASON_APPROVAL_UNAVAILABLE})
