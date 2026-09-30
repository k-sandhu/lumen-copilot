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

Since #502 the gate answers with an :class:`~app.services.tools.types.ApprovalDecision`
rather than a bare bool, so each of the four refusals also names ITSELF; every
case below pins its reason. The end-to-end diagnostic contract (the reason
reaching the model, the trace row and the audit) lives in
``tests/test_run_python_refusal_reasons.py``.
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
from app.domain.tools import (
    APPROVAL_REASON_APPROVAL_UNAVAILABLE,
    APPROVAL_REASON_POLICY_ABSENT,
    APPROVAL_REASON_POLICY_DISABLED,
    APPROVAL_REASON_POLICY_UNREADABLE,
    APPROVAL_SCOPE_TENANT_PREAPPROVAL,
    ERROR_APPROVAL_DENIED,
    RiskTier,
    ToolHandlerResult,
)
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
    decision = await gate.request(_approval_request(world))
    assert decision.approved is False
    assert bool(decision) is False  # truthy iff approved — a bare `if` still denies
    assert decision.reason == APPROVAL_REASON_POLICY_ABSENT


async def test_gate_denies_when_enabled_but_still_requires_approval(world: _World) -> None:
    """Enabled but not pre-approved ⇒ still denied — and it says approval is
    UNAVAILABLE, not that the tool is off (issue #500; see
    ``test_run_python_refusal_reasons`` for the full honesty contract)."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    decision = await gate.request(_approval_request(world))
    assert decision.approved is False
    assert decision.reason == APPROVAL_REASON_APPROVAL_UNAVAILABLE


async def test_gate_denies_when_disabled_even_if_preapproved(world: _World) -> None:
    """A disabled tool is denied even with requires_approval=false (enabled wins)."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=False, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    decision = await gate.request(_approval_request(world))
    assert decision.approved is False
    assert decision.reason == APPROVAL_REASON_POLICY_DISABLED


async def test_gate_allows_when_enabled_and_preapproved(world: _World) -> None:
    """The unlock: enabled AND pre-approved (requires_approval=false) ⇒ allowed."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    decision = await gate.request(_approval_request(world))
    assert decision.approved is True
    # An approval carries no refusal reason — nothing refused.
    assert decision.reason is None


async def test_gate_is_tenant_scoped(world: _World) -> None:
    """Tenant A's pre-approval never approves tenant B's call (INV-1)."""
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    # A gate scoped to tenant B sees no override → deny-by-default.
    gate_b = PolicyApprovalGate(world.session, tenant_id=world.tenant_b_id)
    decision = await gate_b.request(_approval_request(world, tenant_id=world.tenant_b_id))
    assert decision.approved is False
    assert decision.reason == APPROVAL_REASON_POLICY_ABSENT


async def test_gate_fails_closed_on_read_error(world: _World) -> None:
    """An unreadable policy denies — never lets a T2+ action through (fail-closed)."""

    class _BoomSession:
        async def execute(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("db is down")

    gate = PolicyApprovalGate(_BoomSession(), tenant_id=world.tenant_id)  # type: ignore[arg-type]
    decision = await gate.request(_approval_request(world))
    assert decision.approved is False
    # Distinct from "nobody enabled it": this one is an infrastructure fault.
    assert decision.reason == APPROVAL_REASON_POLICY_UNREADABLE


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
    monkeypatch.setattr("app.services.tools.runner.get_tool", lambda name: _gated_tool(calls))
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
    monkeypatch.setattr("app.services.tools.runner.get_tool", lambda name: _gated_tool(calls))
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


# --- #518: INV-7 amended — the pre-approval must actually be RECORDED ----------


async def test_an_approval_names_the_admin_the_policy_row_and_the_call(
    world: _World,
) -> None:
    """The condition on which spec 0004 §2.5 admits a tenant-wide grant (#518).

    INV-7 was amended to accept a tenant-scoped, admin-recorded pre-approval as
    "recorded approval" rather than requiring per-invocation human review. That is a
    real weakening, and it is only defensible if the approval is genuinely recorded —
    so an allow must be able to say WHO granted it, WHICH policy row carries the
    grant, and WHAT call ran under it.

    Before this, `ApprovalDecision.allow()` carried a bare `approved=True`: the audit
    could report that a consequential action was approved and never say by whom.
    """
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)

    decision = await gate.request(_approval_request(world))

    assert decision.approved is True
    record = decision.approval
    assert record is not None, "an approval with no record cannot satisfy the amended INV-7"
    assert record.approved_by == world.user_id
    assert record.policy_id is not None
    assert record.scope == APPROVAL_SCOPE_TENANT_PREAPPROVAL
    # The scope is named, not implied: when #501 lands per-invocation approval, an
    # auditor must be able to tell the two apart in the trail.
    assert record.scope != "per_invocation"


async def test_a_refusal_records_no_approval(world: _World) -> None:
    """The control: a denial authorised nothing, so it must carry no record.

    A record on a refusal would be worse than none — it would put an approval in the
    trail for a call that never ran.
    """
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)

    decision = await gate.request(_approval_request(world))

    assert decision.approved is False
    assert decision.approval is None


async def test_the_recorded_hash_is_the_runner_s_own_not_a_second_one(
    world: _World,
) -> None:
    """One definition of "this call's arguments", not two (#518).

    The runner already computes and audits `args_hash`. Had the gate hashed the
    arguments itself, the trail's `args_hash` and the approval's could drift apart and
    disagree about which call was authorised — the one question the pair exists to
    answer. The gate echoes the value it is given.
    """
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    request = ApprovalRequest(
        call_id="call-1",
        tool_name="run_python",
        risk_tier=RiskTier.T2,
        principal=_principal(world),
        arguments={"code": "print(1)"},
        arguments_hash="the-runners-hash",
    )

    decision = await gate.request(request)

    assert decision.approval is not None
    assert decision.approval.arguments_hash == "the-runners-hash"


async def test_a_deprovisioned_admin_records_none_rather_than_inventing_one(
    world: _World,
) -> None:
    """`updated_by` is SET NULL, so a grant outlives the admin who made it.

    Recording `None` is the honest answer; substituting the caller — who is a member,
    not the authoriser — would put a false attribution in a security trail.
    """
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=None
    )
    await world.session.commit()
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)

    decision = await gate.request(_approval_request(world))

    assert decision.approved is True
    assert decision.approval is not None
    assert decision.approval.approved_by is None
    # The grant itself is still identified, so the trail is not empty.
    assert decision.approval.policy_id is not None


async def test_the_audit_names_who_authorised_an_executed_t2_call(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half of the amendment that carries its weight (#518).

    A record that never reaches the audit trail satisfies nothing: INV-7 is about what
    is RECORDED, not about what a dataclass held for the length of one call. An
    executed T2 invocation must let an auditor go from the call to the admin and the
    policy row that permitted it.
    """
    monkeypatch.setattr("app.services.tools.runner.get_tool", lambda name: _gated_tool([]))
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    runner = _runner(world, PolicyApprovalGate(world.session, tenant_id=world.tenant_id))

    await runner.run(
        call=ToolCall(id="c1", name="run_python", arguments={"code": "print(1)"}),
        context=_context(world),
    )
    await world.session.commit()

    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=20)
    invoked = [e for e in events if e.action == "tool.invoked"]
    assert invoked, "an executed T2 call left no tool.invoked event"
    metadata = invoked[0].metadata
    assert metadata["approval_scope"] == APPROVAL_SCOPE_TENANT_PREAPPROVAL
    assert metadata["approved_by"] == str(world.user_id)
    assert metadata["approval_policy_id"]
    # The call is identified too, so "which approval" and "which call" cannot drift.
    assert metadata["args_hash"]


async def test_a_denied_call_records_no_approver(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal must not put an approval in the trail for a call that never ran."""
    monkeypatch.setattr("app.services.tools.runner.get_tool", lambda name: _gated_tool([]))
    runner = _runner(world, PolicyApprovalGate(world.session, tenant_id=world.tenant_id))

    await runner.run(
        call=ToolCall(id="c1", name="run_python", arguments={}), context=_context(world)
    )
    await world.session.commit()

    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=20)
    invoked = [e for e in events if e.action == "tool.invoked"]
    assert invoked
    assert "approved_by" not in invoked[0].metadata
    assert "approval_scope" not in invoked[0].metadata


async def test_a_prompt_injected_call_in_a_preapproved_tenant_still_executes(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ACCEPTED RESIDUAL RISK of amending INV-7 (#518), pinned deliberately.

    A tenant-wide pre-approval authorises a TOOL, not a payload. Nothing in this path
    distinguishes a call the user asked for from one a retrieved document talked the
    model into making — there is no per-invocation reviewer, which is exactly what
    #501 would add and what spec 0004 §2.5 now says is NOT required.

    This test asserts the behaviour rather than guarding against it, on purpose. The
    risk is accepted, and an accepted risk that nothing exercises is one nobody
    notices when it changes: if a future change makes this call refuse, that is a
    deliberate re-tightening of the invariant and this test should fail and be
    rewritten — not silently keep passing while the trail says something else.

    What the amendment DOES buy is on the last two lines: the execution is attributable
    to the admin who opened the tool, so an incident can name a decision-maker rather
    than ending at "the tenant had it on".
    """
    calls: list[str] = []
    monkeypatch.setattr("app.services.tools.runner.get_tool", lambda name: _gated_tool(calls))
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=False, updated_by=world.user_id
    )
    await world.session.commit()
    runner = _runner(world, PolicyApprovalGate(world.session, tenant_id=world.tenant_id))

    # Arguments a retrieved document induced, not something the user typed.
    injected = ToolCall(
        id="c1",
        name="run_python",
        arguments={"code": "import os; print(os.environ)  # injected by a document"},
    )
    result = await runner.run(call=injected, context=_context(world))
    await world.session.commit()

    assert result.ok is True, "accepted behaviour: a pre-approved tool runs whoever asked"
    assert calls == ["ran"]
    # …and it is attributable.
    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=20)
    invoked = [e for e in events if e.action == "tool.invoked"]
    assert invoked[0].metadata["approved_by"] == str(world.user_id)
