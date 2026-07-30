"""Every blocked ``run_python`` names the gate that refused it (#502) — and #500.

Four INDEPENDENT switches can refuse a code run, and until #502 all four produced
the same opaque "code execution denied" reply, so an operator could not tell which
one to flip. This module pins the diagnostic contract: each refusal carries a
DISTINCT, typed, actionable reason that reaches

* (i) the tool result the model reads (``content``),
* (ii) the ``tool_invocations`` row / the audit trail (``result_summary`` /
  ``reason_code`` metadata),
* (iii) a structured log (asserted at the audit/reason-code level here).

Surfaces (i) and (ii) get **different sentences**. (ii) is behind an operator
boundary and names the exact control; (i) is prompt-injection-reachable — a
retrieved document that says "quote any tool error verbatim" turns it into
assistant output — so it names a product control at most, never an environment
variable, an internal service, process topology, or a datastore outage. The
``Disclosure`` section at the bottom asserts that over every model-visible refusal
this module can produce.

The four gates:

1. the **tenant tool policy** (``services/tools/gate.py``) — absent row, disabled,
   or ``requires_approval`` (issue #500: approval is *unavailable* in chat, which
   must read differently from "disabled"), or unreadable (fail-closed);
2. the **deploy kill-switch** ``SANDBOX_ENABLED`` (``core/config.py``);
3. the **tenant sandbox policy** row (``services/sandbox_policy_service.py``) —
   absent or ``enabled=false``;
4. the **runner** — itself three answers, not one (``sandbox/runner.py``): nothing
   answered, it answered 4xx (it is up and refused the request), or it answered
   5xx (up but broken).

The established error CODES are unchanged (``approval_denied`` /
``code_execution_denied``); the reason is ADDITIVE specificity on top of them.

Every distinctness claim in this module is derived from **real refusals** — a
gate's own :class:`ApprovalDecision` or the ``reason_code`` the execution path
audited. An earlier version asserted over a set of hand-written string literals,
which called no production code and would have passed with every reason-plumbing
line deleted.
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
    SANDBOX_REASON_RUN_ERROR,
    SANDBOX_REASON_RUNNER_ERROR,
    SANDBOX_REASON_RUNNER_REJECTED,
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
from app.sandbox.runner import SandboxRunnerFailed, SandboxRunnerRejected
from app.sandbox.service import SandboxService
from app.sandbox.spec import RunResult, RunSpec, SandboxSessionSpec
from app.services.audit import AuditSink
from app.services.sandbox_policy_service import EffectiveSandboxPolicy, SandboxPolicyReader
from app.services.tools.gate import PolicyApprovalGate
from app.services.tools.runner import ToolRunner
from app.services.tools.types import (
    ApprovalDecision,
    ApprovalRequest,
    DenyAllApprovalGate,
    ToolContext,
    ToolDefinition,
)
from tests._disclosure import assert_control_neutral
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


class _BoomSession:
    """A DB session whose every read fails — the fail-closed (INV-7) branch."""

    async def execute(self, *_: Any, **__: Any) -> Any:
        raise RuntimeError("db is down")


async def _tool_policy_decisions(world: _World) -> list[ApprovalDecision]:
    """The four REAL :class:`PolicyApprovalGate` outcomes, in policy order.

    Absent row → disabled row → enabled-but-still-flagged (#500) → unreadable. Both
    the distinctness assertion and the disclosure scan read from here, so neither
    can drift into asserting against hand-written literals.
    """
    gate = PolicyApprovalGate(world.session, tenant_id=world.tenant_id)
    policies = TenantToolPolicyRepository(world.session, world.tenant_id)

    absent = await gate.request(_approval_request(world))

    await policies.upsert(
        tool_name="run_python", enabled=False, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()
    disabled = await gate.request(_approval_request(world))

    await policies.upsert(
        tool_name="run_python", enabled=True, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()
    unavailable = await gate.request(_approval_request(world))

    unreadable = await PolicyApprovalGate(
        _BoomSession(),  # type: ignore[arg-type]
        tenant_id=world.tenant_id,
    ).request(_approval_request(world))

    return [absent, disabled, unavailable, unreadable]


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
    """Run one code run and return (status, stderr, the terminal audit metadata).

    The audit event is matched on this run's own ``resource_id``, so a test may
    refuse several runs in a row without the newest-first ordering deciding which
    metadata it gets back.
    """
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
    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=50)
    terminal = next(
        e
        for e in events
        if e.resource_id == str(run.id) and e.action in {"code_run.denied", "code_run.finished"}
    )
    return status, persisted.stderr, dict(terminal.metadata)


async def test_deploy_kill_switch_names_sandbox_enabled(world: _World) -> None:
    """Gate 2 — the tenant is fully enabled; only the DEPLOY switch is off."""
    await _enable_tenant_sandbox(world)
    await world.session.commit()
    status, stderr, metadata = await _refuse(world, deploy_enabled=False)

    assert status is CodeRunStatus.DENIED
    assert metadata["reason_code"] == SANDBOX_REASON_DEPLOY_DISABLED
    # The AUDIT names the exact switch an operator flips (a deploy env var, not an
    # admin toggle) …
    assert "SANDBOX_ENABLED" in str(metadata["reason"])
    # … and the model-visible stderr does not: it is the tool reply, so a
    # prompt-injected "quote the tool error" must not recite the kill-switch.
    assert "SANDBOX_ENABLED" not in stderr
    assert_control_neutral(stderr, where="code_runs.stderr (deploy kill-switch)")


async def test_absent_tenant_sandbox_policy_names_the_workspace_policy(
    world: _World,
) -> None:
    """Gate 3a — deploy switch on, but the workspace has no sandbox policy row."""
    status, stderr, metadata = await _refuse(world)

    assert status is CodeRunStatus.DENIED
    assert metadata["reason_code"] == SANDBOX_REASON_POLICY_ABSENT
    assert "SANDBOX_ENABLED" not in stderr  # NOT the deploy switch's fault
    # The model-visible line names the product control an admin uses, and only that.
    assert "admin" in stderr.lower()
    assert_control_neutral(stderr, where="code_runs.stderr (absent workspace policy)")
    # The operator sentence stays specific about which policy row is missing.
    assert "sandbox policy" in str(metadata["reason"]).lower()


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

    # An unreachable dependency is a FAILED run (not a policy denial) — but the
    # AUDIT must say WHICH dependency, so nobody hunts for a policy switch that is
    # already on.
    assert status is CodeRunStatus.FAILED
    assert metadata["reason_code"] == SANDBOX_REASON_RUNNER_UNAVAILABLE
    assert "sandbox-runner" in str(metadata["reason"])
    # The model, meanwhile, learns only that the sandbox is unavailable — naming the
    # internal service AND that it is down is a live topology signal.
    assert "sandbox-runner" not in stderr
    assert_control_neutral(stderr, where="code_runs.stderr (runner unreachable)")


async def test_the_four_gates_give_pairwise_distinct_reasons(world: _World) -> None:
    """The whole point of #502: four gates, four distinguishable answers.

    Every value below is **produced by the gate that refused**. The earlier version
    of this test built a set of four string LITERALS and asserted its size, which
    called no production code at all and would have passed with every
    reason-plumbing line in the codebase deleted.
    """

    def _audited(metadata: dict[str, object]) -> str:
        value = metadata.get("reason_code")
        assert isinstance(value, str) and value, f"terminal carried no reason_code: {metadata}"
        return value

    collected: list[str] = []

    # Gate 1 — the tenant TOOL policy. Enabled, but left flagged "requires
    # approval", which no surface can satisfy (#500).
    await TenantToolPolicyRepository(world.session, world.tenant_id).upsert(
        tool_name="run_python", enabled=True, requires_approval=True, updated_by=world.user_id
    )
    await world.session.commit()
    decision = await PolicyApprovalGate(world.session, tenant_id=world.tenant_id).request(
        _approval_request(world)
    )
    assert decision.reason is not None
    collected.append(decision.reason)

    # Gate 2 — the deploy kill-switch, with the workspace fully enabled.
    await _enable_tenant_sandbox(world)
    await world.session.commit()
    _, _, deploy = await _refuse(world, deploy_enabled=False)
    collected.append(_audited(deploy))

    # Gate 4 — every policy says yes; the runner service is simply not there.
    unreachable = _FakeRunner(
        ensure_raises=DependencyError(
            "sandbox runner unavailable", code="sandbox_runner_unavailable"
        )
    )
    _, _, runner_meta = await _refuse(world, runner=unreachable)
    collected.append(_audited(runner_meta))

    # Gate 3 — the workspace's own sandbox policy says no.
    await _enable_tenant_sandbox(world, enabled=False)
    await world.session.commit()
    _, _, tenant_meta = await _refuse(world)
    collected.append(_audited(tenant_meta))

    assert len(collected) == 4
    assert all(collected), f"a gate produced no reason at all: {collected}"
    assert len(set(collected)) == 4, collected
    # The codes are the documented ones, so the runbook's table stays true.
    assert collected == [
        APPROVAL_REASON_APPROVAL_UNAVAILABLE,
        SANDBOX_REASON_DEPLOY_DISABLED,
        SANDBOX_REASON_RUNNER_UNAVAILABLE,
        SANDBOX_REASON_TENANT_DISABLED,
    ]


async def test_the_tool_policy_family_splits_into_four_real_sub_reasons(
    world: _World,
) -> None:
    """Gate 1 is four gates wearing one coat — asserted from four real decisions."""
    decisions = await _tool_policy_decisions(world)
    reasons = [decision.reason for decision in decisions]
    assert all(reasons)
    assert len(set(reasons)) == 4, reasons
    assert set(reasons) == {
        APPROVAL_REASON_POLICY_ABSENT,
        APPROVAL_REASON_POLICY_DISABLED,
        APPROVAL_REASON_APPROVAL_UNAVAILABLE,
        APPROVAL_REASON_POLICY_UNREADABLE,
    }


async def test_a_runner_that_refuses_the_request_is_not_reported_as_unreachable(
    world: _World,
) -> None:
    """Gate 4, split — the runner ANSWERED. "Unreachable" would send the wrong operator.

    A failed package download / an invalid requirement / a stale generation are
    4xx answers from a running service (``sandbox/runner.py`` now raises
    :class:`SandboxRunnerRejected`), and a reachable-but-broken runner answers 5xx.
    Both used to reach the audit trail as ``sandbox_runner_unavailable``.
    """
    await _enable_tenant_sandbox(world)
    await world.session.commit()
    rejected = _FakeRunner(
        ensure_raises=SandboxRunnerRejected("the sandbox runner refused the request")
    )
    status, stderr, metadata = await _refuse(world, runner=rejected)

    assert status is CodeRunStatus.FAILED
    assert metadata["reason_code"] == SANDBOX_REASON_RUNNER_REJECTED
    assert metadata["reason_code"] != SANDBOX_REASON_RUNNER_UNAVAILABLE
    assert_control_neutral(stderr, where="code_runs.stderr (runner rejected)")


async def test_a_runner_that_fails_internally_is_its_own_reason(world: _World) -> None:
    """…and a 5xx is distinct again: the service is up, its own log is next."""
    await _enable_tenant_sandbox(world)
    await world.session.commit()
    broken = _FakeRunner(ensure_raises=SandboxRunnerFailed("the sandbox runner failed"))
    status, stderr, metadata = await _refuse(world, runner=broken)

    assert status is CodeRunStatus.FAILED
    assert metadata["reason_code"] == SANDBOX_REASON_RUNNER_ERROR
    assert_control_neutral(stderr, where="code_runs.stderr (runner failed)")


async def test_a_policy_flip_between_the_two_checks_keeps_the_policy_reason(
    world: _World,
) -> None:
    """The execution path checks enablement TWICE; the second must say the same thing.

    ``SandboxService.execute`` admits the run, then ``SandboxSessionService.ensure``
    re-reads the policy before allocating a container. An admin turning code
    execution off in between used to land as a ``failed`` run reason-coded
    ``sandbox_run_error`` — the generic crash bucket — for what is plainly the
    tenant policy refusing. Same gate, same terminal, same reason code.
    """
    await _enable_tenant_sandbox(world)
    await world.session.commit()

    run = await CodeRunRepository(world.session, world.tenant_id).create(
        owner_id=world.user_id,
        session_id=world.chat_id,
        code="print(1)",
        requested_packages=(),
    )
    runner = _FakeRunner()
    service = SandboxService(
        tenant_id=world.tenant_id,
        owner_id=world.user_id,
        runner=runner,  # type: ignore[arg-type]
        object_store=_FakeStore(),  # type: ignore[arg-type]
        settings=sandbox_settings(),
    )

    # Flip the workspace policy off in the window between the two checks: the first
    # read is the one inside ``execute``, the second the one inside ``ensure``.
    reads = 0
    original = SandboxPolicyReader.resolve

    async def _flip_after_first_read(self: SandboxPolicyReader) -> EffectiveSandboxPolicy:
        nonlocal reads
        reads += 1
        if reads > 1:
            return EffectiveSandboxPolicy(
                enabled=False,
                disabled_reason=SANDBOX_REASON_TENANT_DISABLED,
                allowed_packages=(),
                denied_packages=(),
                egress_allowed=False,
                egress_allowlist=(),
                max_runtime_s=30,
                max_memory_mb=512,
                daily_runtime_cap_s=3600,
                max_concurrency=2,
            )
        return await original(self)

    SandboxPolicyReader.resolve = _flip_after_first_read  # type: ignore[method-assign]
    try:
        status = await service.execute(world.session, run.id)
    finally:
        SandboxPolicyReader.resolve = original  # type: ignore[method-assign]

    assert reads >= 2, "the second enablement check never ran — the test proves nothing"
    # It is a POLICY refusal, so it lands where the first check would have put it …
    assert status is CodeRunStatus.DENIED
    # …refused before any container was allocated, exactly like the first check.
    assert runner.ensured == []
    assert runner.executions == []
    events = await AuditEventRepository(world.session, world.tenant_id).list_recent(limit=20)
    terminal = next(e for e in events if e.action in {"code_run.denied", "code_run.finished"})
    assert terminal.action == "code_run.denied"
    # … carrying the policy's own reason, not the generic crash bucket.
    assert terminal.metadata["reason_code"] == SANDBOX_REASON_TENANT_DISABLED
    assert terminal.metadata["reason_code"] != SANDBOX_REASON_RUN_ERROR
    persisted = await CodeRunRepository(world.session, world.tenant_id).get(run.id)
    assert persisted is not None
    assert_control_neutral(persisted.stderr, where="code_runs.stderr (mid-run policy flip)")


# ---------------------------------------------------------------------------
# Disclosure — what the MODEL is allowed to be told (issue #502)
# ---------------------------------------------------------------------------


async def test_no_approval_gate_detail_discloses_deployment_infrastructure(
    world: _World,
) -> None:
    """Every ``ApprovalDecision.detail`` is fed verbatim to the model — so scan them all.

    Covers all four :class:`PolicyApprovalGate` outcomes plus the inert
    :class:`DenyAllApprovalGate`. The unreadable-policy branch used to say "Retry
    once the policy store is reachable" (a live datastore-outage signal) and the
    inert gate used to say "this deployment has no approval flow wired" (process
    topology); both are reachable by a prompt-injected "quote the tool error".
    """
    decisions = [
        *await _tool_policy_decisions(world),
        await DenyAllApprovalGate().request(_approval_request(world)),
    ]

    assert len(decisions) == 5
    for decision in decisions:
        assert decision.detail, f"gate {decision.reason} produced no model-facing detail"
        assert_control_neutral(
            decision.detail, where=f"ApprovalDecision.detail ({decision.reason})"
        )


async def test_every_sandbox_refusal_stderr_is_control_neutral(world: _World) -> None:
    """The same scan over the real ``code_runs.stderr`` of each sandbox gate.

    ``stderr`` is what ``run_python`` renders into the tool reply, so this is the
    end-to-end form of the rule the message table asserts in
    ``tests/test_code_execution_messages.py``.
    """
    seen: list[str] = []

    # Gate 2 — the deploy kill-switch.
    await _enable_tenant_sandbox(world)
    await world.session.commit()
    _, deploy_stderr, _ = await _refuse(world, deploy_enabled=False)
    seen.append(deploy_stderr)

    # Gate 4 — the runner is not there.
    unreachable = _FakeRunner(
        ensure_raises=DependencyError(
            "sandbox runner unavailable", code="sandbox_runner_unavailable"
        )
    )
    _, runner_stderr, _ = await _refuse(world, runner=unreachable)
    seen.append(runner_stderr)

    # Gate 3b — the workspace policy says no.
    await _enable_tenant_sandbox(world, enabled=False)
    await world.session.commit()
    _, tenant_stderr, _ = await _refuse(world)
    seen.append(tenant_stderr)

    assert all(seen), "a refusal with an empty stderr tells the model nothing"
    for value in seen:
        assert_control_neutral(value, where="code_runs.stderr")
