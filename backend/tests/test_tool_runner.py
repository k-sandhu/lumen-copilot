"""Governed tool runner — the CC-7 chokepoint (issue #207, negative-first).

Drives :class:`~app.services.tools.runner.ToolRunner` offline (in-memory SQLite for
the ``tool_invocations`` + ``audit_events`` writes; a fake retrieval so no pgvector
is needed) and asserts the whole governance contract as **negative** tests:

* AC-2 (INV-style): an off-allow-list tool → ``tool_not_permitted``; the handler
  never runs and the run continues.
* AC-3 / INV-7: a ``requires_approval`` tool blocks on the approval gate — an
  unapproved call is **not executed** (``approval_denied``); a T0/T1 tool bypasses
  the gate; an *approved* gated call does run.
* AC-4 / INV-6: **every** invocation (success, denial, failure) writes a
  ``tool_invocations`` row and emits ``tool.invoked`` + ``tool.result``.
* AC-5: a handler that raises / times out → an ``ok=False`` result, not a crash.
* AC-N: an unknown tool → ``tool_not_found`` (deny by default); the args hash is
  recorded, never the raw args.
"""

from __future__ import annotations

import asyncio
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
    ToolInvocationRepository,
    UserRepository,
)
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, AutonomyLevel, Role
from app.domain.llm import ToolCall
from app.domain.tools import (
    ERROR_APPROVAL_DENIED,
    ERROR_AUTONOMY_DENIED,
    ERROR_NOT_FOUND,
    ERROR_NOT_PERMITTED,
    ERROR_TOOL_ERROR,
    ERROR_TOOL_TIMEOUT,
    RiskTier,
    ToolHandlerResult,
)
from app.services.audit import AuditSink
from app.services.tools import runner as runner_module
from app.services.tools.runner import ToolRunner, hash_args
from app.services.tools.types import ApprovalRequest, ToolContext, ToolDefinition

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


# --- Fakes / fixtures -------------------------------------------------------


class _FakeRetrieval:
    """A retrieval stand-in; the governance tests never actually search."""


class _World:
    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
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
            user = await UserRepository(session, tenant.id).create(
                email="alice@acme.test", password_hash="x", roles=[Role.MEMBER]
            )
            await session.commit()
            yield _World(session=session, tenant_id=tenant.id, user_id=user.id)
    finally:
        await engine.dispose()


def _principal(w: _World) -> Principal:
    return Principal(user_id=w.user_id, tenant_id=w.tenant_id, roles=(Role.MEMBER,))


def _context(w: _World) -> ToolContext:
    return ToolContext(principal=_principal(w), retrieval=_FakeRetrieval())  # type: ignore[arg-type]


def _make_runner(
    w: _World,
    *,
    allowed: frozenset[str],
    gate: Any | None = None,
    autonomy: AutonomyLevel = AutonomyLevel.ACT_AUTO,
) -> tuple[ToolRunner, AuditEventRepository, ToolInvocationRepository]:
    audit_repo = AuditEventRepository(w.session, w.tenant_id)
    inv_repo = ToolInvocationRepository(w.session, w.tenant_id)
    r = ToolRunner(
        allowed=allowed,
        invocations=inv_repo,
        audit=AuditSink(audit_repo),
        actor=AuditActor.user(w.user_id),
        request_id="req-1",
        source_ip="127.0.0.1",
        session_id=None,
        gate=gate,
        autonomy=autonomy,
    )
    return r, audit_repo, inv_repo


# A registrable tool factory: each returns a ToolDefinition whose handler does a
# controlled thing (ok / raise / hang) so we can exercise every runner branch by
# monkeypatching the registry's ``get_tool`` to return it.


def _ok_tool(name: str = "probe") -> ToolDefinition:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        return ToolHandlerResult(content="ok", summary="did it", payload={"echo": args})

    return ToolDefinition(
        name=name, description="d", json_schema={"type": "object"}, handler=handler
    )


def _raising_tool(name: str = "boom") -> ToolDefinition:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        raise RuntimeError("kaboom — vendor detail that must not leak")

    return ToolDefinition(
        name=name, description="d", json_schema={"type": "object"}, handler=handler
    )


def _slow_tool(name: str = "slow") -> ToolDefinition:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        await asyncio.sleep(1.0)
        return ToolHandlerResult(content="too late")

    return ToolDefinition(
        name=name,
        description="d",
        json_schema={"type": "object"},
        handler=handler,
        timeout_seconds=0.02,
    )


def _gated_tool(name: str = "send_email") -> ToolDefinition:
    """A T2 write-tier tool that requires approval (out of MVP; the seam under test)."""

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        return ToolHandlerResult(content="sent!", summary="sent")

    return ToolDefinition(
        name=name,
        description="d",
        json_schema={"type": "object"},
        handler=handler,
        risk_tier=RiskTier.T2,
        requires_approval=True,
        read_only=False,
    )


def _t1_tool(name: str = "write_note") -> ToolDefinition:
    """A T1 side-effecting tool (write-a-file-class) — the tier autonomy gates (#218).

    Not read-only, T1, no approval required at the registry level (T1 does not
    ``requires_approval`` — that is a T2+ property). Whether it may run is decided by
    the run's EFFECTIVE autonomy: suggest/draft deny it, act_with_approval routes it
    through the approval seam, act_auto executes it.
    """

    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        return ToolHandlerResult(content="wrote it", summary="wrote")

    return ToolDefinition(
        name=name,
        description="d",
        json_schema={"type": "object"},
        handler=handler,
        risk_tier=RiskTier.T1,
        requires_approval=False,
        read_only=False,
    )


def _patch_tool(monkeypatch: pytest.MonkeyPatch, definition: ToolDefinition) -> None:
    """Route the runner's registry lookup to ``definition`` for its name."""

    def fake_get_tool(name: str) -> ToolDefinition:
        if name == definition.name:
            return definition
        from app.services.tools.registry import UnknownToolError

        raise UnknownToolError(name)

    monkeypatch.setattr(runner_module, "get_tool", fake_get_tool)


async def _audit_actions(audit_repo: AuditEventRepository) -> list[str]:
    events = await audit_repo.list_recent(limit=100)
    return [e.action for e in events]


# --- AC-N: unknown tool → deny by default -----------------------------------


async def test_unknown_tool_is_denied_and_recorded(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No tool registered for this name → tool_not_found, run continues.
    _patch_tool(monkeypatch, _ok_tool("probe"))
    r, audit_repo, _ = _make_runner(world, allowed=frozenset({"probe"}))
    result = await r.run(
        call=ToolCall(id="c1", name="does_not_exist", arguments={}), context=_context(world)
    )
    assert result.ok is False
    assert result.error == ERROR_NOT_FOUND
    # Still audited + traced (INV-6): both events + one row, outcome denied.
    actions = await _audit_actions(audit_repo)
    assert actions.count(AuditAction.TOOL_INVOKED.value) == 1
    assert actions.count(AuditAction.TOOL_RESULT.value) == 1
    invocations = await _all_invocations(world)
    assert len(invocations) == 1
    assert invocations[0].ok is False and invocations[0].error == ERROR_NOT_FOUND


async def test_runner_records_increasing_ordinals_per_turn(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each call in one runner lifetime (= one answer turn) gets the next
    ordinal, so the persisted trace is genuinely oldest-first even when every
    row shares the transaction timestamp (#397)."""
    _patch_tool(monkeypatch, _ok_tool("probe"))
    r, _, _ = _make_runner(world, allowed=frozenset({"probe"}))
    for i in range(3):
        await r.run(call=ToolCall(id=f"c{i}", name="probe", arguments={}), context=_context(world))
    invocations = await _all_invocations(world)
    assert sorted(inv.ordinal for inv in invocations) == [0, 1, 2]


# --- AC-2 (INV-style): off-allow-list tool → tool_not_permitted -------------


async def test_off_allowlist_tool_is_not_permitted_and_handler_never_runs(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``probe`` is registered and would succeed — but it is NOT in the allow-list.
    _patch_tool(monkeypatch, _ok_tool("probe"))
    r, audit_repo, _ = _make_runner(world, allowed=frozenset({"something_else"}))
    result = await r.run(
        call=ToolCall(id="c1", name="probe", arguments={"x": 1}), context=_context(world)
    )
    assert result.ok is False
    assert result.error == ERROR_NOT_PERMITTED
    # The run continues: the caller gets a result, not an exception. And it is
    # audited denied + traced.
    events = await audit_repo.list_recent(limit=10)
    assert {e.outcome for e in events} == {AuditOutcome.DENIED}
    invocations = await _all_invocations(world)
    assert len(invocations) == 1 and invocations[0].error == ERROR_NOT_PERMITTED


async def test_on_allowlist_tool_runs(world: _World, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_tool(monkeypatch, _ok_tool("probe"))
    r, audit_repo, _ = _make_runner(world, allowed=frozenset({"probe"}))
    result = await r.run(
        call=ToolCall(id="c1", name="probe", arguments={"x": 1}), context=_context(world)
    )
    assert result.ok is True
    assert result.content == "ok"
    events = await audit_repo.list_recent(limit=10)
    assert {e.outcome for e in events} == {AuditOutcome.ALLOWED}


# --- AC-3 / INV-7: approval seam --------------------------------------------


async def test_requires_approval_tool_blocks_when_unapproved(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A T2 gated tool with the default deny-all gate: NOT executed (INV-7).
    gated = _gated_tool("send_email")
    _patch_tool(monkeypatch, gated)
    r, audit_repo, _ = _make_runner(world, allowed=frozenset({"send_email"}))  # allowed, but gated
    result = await r.run(
        call=ToolCall(id="c1", name="send_email", arguments={"to": "x@y.z"}),
        context=_context(world),
    )
    assert result.ok is False
    assert result.error == ERROR_APPROVAL_DENIED
    # "sent!" must NOT appear — the handler never ran.
    assert "sent" not in result.content.lower() or "not performed" in result.content.lower()
    invocations = await _all_invocations(world)
    assert len(invocations) == 1 and invocations[0].error == ERROR_APPROVAL_DENIED


async def test_requires_approval_tool_runs_when_approved(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _ApproveAll:
        async def request(self, request: ApprovalRequest) -> bool:
            return True

    _patch_tool(monkeypatch, _gated_tool("send_email"))
    r, _, _ = _make_runner(world, allowed=frozenset({"send_email"}), gate=_ApproveAll())
    result = await r.run(
        call=ToolCall(id="c1", name="send_email", arguments={}), context=_context(world)
    )
    assert result.ok is True and result.content == "sent!"


async def test_t0_tool_bypasses_the_gate(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A gate that records whether it was consulted. A T0 (read-only) tool must
    # NEVER reach it.
    consulted: list[str] = []

    class _RecordingGate:
        async def request(self, request: ApprovalRequest) -> bool:
            consulted.append(request.tool_name)
            return False

    _patch_tool(monkeypatch, _ok_tool("probe"))  # T0, read_only, no approval
    r, _, _ = _make_runner(world, allowed=frozenset({"probe"}), gate=_RecordingGate())
    result = await r.run(
        call=ToolCall(id="c1", name="probe", arguments={}), context=_context(world)
    )
    assert result.ok is True
    assert consulted == []  # the gate was bypassed for the T0 tool


# --- #218: autonomy gates side-effecting (T1) tools by the effective level ---


@pytest.mark.parametrize("autonomy", [AutonomyLevel.SUGGEST, AutonomyLevel.DRAFT])
async def test_t1_tool_denied_below_act_autonomy(
    world: _World, monkeypatch: pytest.MonkeyPatch, autonomy: AutonomyLevel
) -> None:
    """AC-1 / AC-N (#218, INV-7-style): a suggest/draft assistant CANNOT run a T1 write.

    The negative: a draft-level assistant may only suggest/draft — a file-write is
    refused (``autonomy_denied``) BEFORE the handler runs, and the denial is audited.
    """
    consulted: list[str] = []

    class _RecordingGate:
        async def request(self, request: ApprovalRequest) -> bool:
            consulted.append(request.tool_name)
            return True  # even a permissive gate must not rescue a below-level T1 tool

    _patch_tool(monkeypatch, _t1_tool("write_note"))
    r, audit_repo, _ = _make_runner(
        world, allowed=frozenset({"write_note"}), gate=_RecordingGate(), autonomy=autonomy
    )
    result = await r.run(
        call=ToolCall(id="c1", name="write_note", arguments={"text": "x"}),
        context=_context(world),
    )
    # The side effect is refused by the autonomy level itself, not the approval gate.
    assert result.ok is False
    assert result.error == ERROR_AUTONOMY_DENIED
    assert consulted == []  # the handler + the gate were never reached
    # Audited + traced denied (INV-6) — a refusal is never a silent drop.
    events = await audit_repo.list_recent(limit=10)
    assert {e.outcome for e in events} == {AuditOutcome.DENIED}
    invocations = await _all_invocations(world)
    assert len(invocations) == 1
    assert invocations[0].ok is False and invocations[0].error == ERROR_AUTONOMY_DENIED


async def test_t1_tool_runs_at_act_auto(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1 (#218): at ``act_auto`` a T1 write executes automatically (no gate)."""
    consulted: list[str] = []

    class _RecordingGate:
        async def request(self, request: ApprovalRequest) -> bool:
            consulted.append(request.tool_name)
            return False

    _patch_tool(monkeypatch, _t1_tool("write_note"))
    r, _, _ = _make_runner(
        world,
        allowed=frozenset({"write_note"}),
        gate=_RecordingGate(),
        autonomy=AutonomyLevel.ACT_AUTO,
    )
    result = await r.run(
        call=ToolCall(id="c1", name="write_note", arguments={}), context=_context(world)
    )
    assert result.ok is True and result.content == "wrote it"
    # act_auto executes automatically — the approval gate is NOT consulted for a T1 tool.
    assert consulted == []


async def test_t1_tool_routes_through_approval_at_act_with_approval(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-1 (#218, INV-7): at ``act_with_approval`` a T1 write is gated by approval.

    Denied when the gate refuses; executed only when the gate approves — the same seam
    a T2+ tool uses, now reached for a T1 tool at this autonomy level.
    """
    # (a) an unapproving gate → the T1 write is denied (approval_denied).
    class _DenyAll:
        async def request(self, request: ApprovalRequest) -> bool:
            return False

    _patch_tool(monkeypatch, _t1_tool("write_note"))
    r, _, _ = _make_runner(
        world,
        allowed=frozenset({"write_note"}),
        gate=_DenyAll(),
        autonomy=AutonomyLevel.ACT_WITH_APPROVAL,
    )
    denied = await r.run(
        call=ToolCall(id="c1", name="write_note", arguments={}), context=_context(world)
    )
    assert denied.ok is False and denied.error == ERROR_APPROVAL_DENIED

    # (b) an approving gate → the T1 write executes.
    class _ApproveAll:
        async def request(self, request: ApprovalRequest) -> bool:
            return True

    r2, _, _ = _make_runner(
        world,
        allowed=frozenset({"write_note"}),
        gate=_ApproveAll(),
        autonomy=AutonomyLevel.ACT_WITH_APPROVAL,
    )
    approved = await r2.run(
        call=ToolCall(id="c2", name="write_note", arguments={}), context=_context(world)
    )
    assert approved.ok is True and approved.content == "wrote it"


async def test_t0_tool_never_autonomy_gated(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#218: a read-only T0 tool runs even at the lowest autonomy (only T1 is gated)."""
    _patch_tool(monkeypatch, _ok_tool("probe"))  # T0 read-only
    r, _, _ = _make_runner(
        world, allowed=frozenset({"probe"}), autonomy=AutonomyLevel.SUGGEST
    )
    result = await r.run(
        call=ToolCall(id="c1", name="probe", arguments={}), context=_context(world)
    )
    assert result.ok is True and result.content == "ok"


# --- AC-5: a raising / slow tool yields ok=False, never a crash -------------


async def test_raising_tool_becomes_ok_false_result(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tool(monkeypatch, _raising_tool("boom"))
    r, _, _ = _make_runner(world, allowed=frozenset({"boom"}))
    result = await r.run(call=ToolCall(id="c1", name="boom", arguments={}), context=_context(world))
    assert result.ok is False
    assert result.error == ERROR_TOOL_ERROR
    # The vendor detail from the exception must NOT leak into the model-facing reply.
    assert "kaboom" not in result.content
    invocations = await _all_invocations(world)
    assert invocations[0].ok is False and invocations[0].error == ERROR_TOOL_ERROR


async def test_slow_tool_times_out_to_ok_false(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tool(monkeypatch, _slow_tool("slow"))
    r, _, _ = _make_runner(world, allowed=frozenset({"slow"}))
    result = await r.run(call=ToolCall(id="c1", name="slow", arguments={}), context=_context(world))
    assert result.ok is False
    assert result.error == ERROR_TOOL_TIMEOUT


# --- AC-4 / INV-6: every invocation is audited + traced ---------------------


async def test_success_writes_both_audit_events_and_a_trace_row(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tool(monkeypatch, _ok_tool("probe"))
    r, audit_repo, _ = _make_runner(world, allowed=frozenset({"probe"}))
    await r.run(
        call=ToolCall(id="c1", name="probe", arguments={"q": "hi"}), context=_context(world)
    )
    actions = await _audit_actions(audit_repo)
    assert AuditAction.TOOL_INVOKED.value in actions
    assert AuditAction.TOOL_RESULT.value in actions
    invocations = await _all_invocations(world)
    assert len(invocations) == 1
    row = invocations[0]
    assert row.tool_name == "probe"
    # The row stores the ARG HASH, never the raw args (spec 0004 §2.4).
    assert row.args_hash == hash_args({"q": "hi"})
    assert "hi" not in row.args_hash


async def test_args_hash_is_order_independent() -> None:
    assert hash_args({"a": 1, "b": 2}) == hash_args({"b": 2, "a": 1})
    assert hash_args({"a": 1}) != hash_args({"a": 2})


# --- helpers ----------------------------------------------------------------


async def _all_invocations(w: _World) -> list[Any]:
    """Read every tool_invocations row for the tenant (test-only, direct query)."""
    from sqlalchemy import select

    from app.db import models

    stmt = select(models.ToolInvocation).where(models.ToolInvocation.tenant_id == w.tenant_id)
    rows = (await w.session.execute(stmt)).scalars().all()
    return list(rows)
