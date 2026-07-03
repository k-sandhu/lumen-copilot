"""The policy-driven approval gate (issue #223) — the run_python unlock, both ways.

Drives :class:`~app.services.tools.gate.PolicyApprovalGate` and the governed
:class:`~app.services.tools.runner.ToolRunner` offline (in-memory SQLite for the
``tenant_tool_policy`` + ``tool_invocations`` + ``audit_events`` writes; a fake
retrieval so no pgvector is needed). The point of the issue is proven **both ways**:

* Deny-by-default (INV-7): a ``requires_approval`` tool with NO admin policy row is
  denied — the same behaviour as the old fail-closed ``DenyAllApprovalGate``.
* The unlock: once an admin enables AND pre-approves the tool for the tenant
  (``enabled=true`` + ``requires_approval=false``), the gate allows it and the
  handler runs.
* Fail-closed on a read error: an unreadable policy denies (never allows a T2+
  action on a policy it could not read).
* Cross-tenant isolation (INV-1): tenant A's pre-approval never enables tenant B's
  tool.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.auth.principal import Principal
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    TenantRepository,
    TenantToolPolicyRepository,
    ToolInvocationRepository,
    UserRepository,
)
from app.domain.audit import AuditActor
from app.domain.entities import Role
from app.domain.llm import ToolCall
from app.domain.tools import ERROR_APPROVAL_DENIED, RiskTier, ToolHandlerResult
from app.services.audit import AuditSink
from app.services.tools.gate import PolicyApprovalGate
from app.services.tools.runner import ToolRunner
from app.services.tools.types import ApprovalRequest, ToolContext, ToolDefinition

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


class _FakeRetrieval:
    """A retrieval stand-in; the gate tests never actually search."""


class _World:
    def __init__(
        self,
        *,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        tenant_b_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.tenant_b_id = tenant_b_id
        self.user_id = user_id


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
            tenant_b = await TenantRepository(session).create(name="Globex")
            user = await UserRepository(session, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.ADMIN]
            )
            await session.commit()
            yield _World(
                session=session,
                tenant_id=tenant.id,
                tenant_b_id=tenant_b.id,
                user_id=user.id,
            )
    finally:
        await engine.dispose()


def _principal(world: _World, *, tenant_id: uuid.UUID | None = None) -> Principal:
    return Principal(
        user_id=world.user_id,
        tenant_id=tenant_id or world.tenant_id,
        roles=(Role.ADMIN,),
    )


def _approval_request(world: _World, *, tenant_id: uuid.UUID | None = None) -> ApprovalRequest:
    return ApprovalRequest(
        call_id="call-1",
        tool_name="run_python",
        risk_tier=RiskTier.T2,
        principal=_principal(world, tenant_id=tenant_id),
        arguments={"code": "print(1)"},
    )


# --- The gate directly ------------------------------------------------------


async def test_gate_denies_when_no_policy_row(world: _World) -> None:
    """Deny-by-default: a gated tool with no admin override is denied (INV-7)."""
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    assert await gate.request(_approval_request(world)) is False


async def test_gate_denies_when_enabled_but_still_requires_approval(world: _World) -> None:
    """Enabled but not pre-approved ⇒ still denied (approval is required, no reviewer)."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    assert await gate.request(_approval_request(world)) is False


async def test_gate_denies_when_disabled_even_if_preapproved(world: _World) -> None:
    """A disabled tool is denied even with requires_approval=false (enabled wins)."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=False, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    assert await gate.request(_approval_request(world)) is False


async def test_gate_allows_when_enabled_and_preapproved(world: _World) -> None:
    """The unlock: enabled AND pre-approved (requires_approval=false) ⇒ allowed."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    assert await gate.request(_approval_request(world)) is True


async def test_gate_is_tenant_scoped(world: _World) -> None:
    """Tenant A's pre-approval never approves tenant B's call (INV-1)."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    # A gate scoped to tenant B sees no override → deny-by-default.
    gate_b = PolicyApprovalGate(world.session, tenant_id=world.tenant_b_id)
    assert await gate_b.request(_approval_request(world, tenant_id=world.tenant_b_id)) is False


async def test_gate_fails_closed_on_read_error(world: _World) -> None:
    """An unreadable policy denies — never lets a T2+ action through (fail-closed)."""

    class _BoomSession:
        async def execute(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("db is down")

    gate = PolicyApprovalGate(_BoomSession(), tenant_id=world.tenant_id)  # type: ignore[arg-type]
    assert await gate.request(_approval_request(world)) is False


# --- End-to-end through the governed runner (the observable unlock) ---------


def _gated_tool(calls: list[str]) -> ToolDefinition:
    """A T2 requires_approval tool whose handler records that it actually ran."""

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


def _runner(world: _World, gate: PolicyApprovalGate) -> ToolRunner:
    return ToolRunner(
        allowed=frozenset({"run_python"}),
        invocations=ToolInvocationRepository(world.session, world.tenant_id),
        audit=AuditSink(AuditEventRepository(world.session, world.tenant_id)),
        actor=AuditActor.user(world.user_id),
        request_id="req-1",
        source_ip="127.0.0.1",
        gate=gate,
    )


def _context(world: _World) -> ToolContext:
    return ToolContext(principal=_principal(world), retrieval=_FakeRetrieval())  # type: ignore[arg-type]


async def test_runner_denies_gated_tool_without_policy(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run_python negative: no admin policy ⇒ approval_denied, handler never runs."""
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.tools.runner.get_tool", lambda name: _gated_tool(calls)
    )
    runner = _runner(world, PolicyApprovalGate(world.session, tenant_id=world.tenant_id))
    result = await runner.run(
        call=ToolCall(id="c1", name="run_python", arguments={}), context=_context(world)
    )
    assert result.ok is False
    assert result.error == ERROR_APPROVAL_DENIED
    assert calls == []  # the handler did NOT execute


async def test_runner_executes_gated_tool_once_admin_preapproves(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run_python unlock: admin enables + pre-approves ⇒ the handler runs."""
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.tools.runner.get_tool", lambda name: _gated_tool(calls)
    )
    # The admin turns run_python on for the tenant (enabled + pre-approved).
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()

    runner = _runner(world, PolicyApprovalGate(world.session, tenant_id=world.tenant_id))
    result = await runner.run(
        call=ToolCall(id="c1", name="run_python", arguments={}), context=_context(world)
    )
    assert result.ok is True
    assert calls == ["ran"]  # the handler DID execute (the unlock)
