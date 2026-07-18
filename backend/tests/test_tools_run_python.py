"""The ``run_python`` tool — the governed code-execution capability (issue #231).

Drives :mod:`app.services.tools.impls.run_python` **offline** with a fake sandbox
seam (a :class:`~app.services.tools.types.SandboxToolRunner`) so no container engine
is needed — the real isolation is #230's live-gated coverage. Asserts the whole #231
contract negative-first:

* **Governance / deny-by-default** — the tool is discoverable, **T2**, ``read_only``
  False, ``requires_approval`` True, and **absent** from the default ad-hoc allow-list
  (``registry.default_allowlist``); enabling it is a deliberate allow-list + admin
  decision.
* **AC-1** — a successful submission returns an ``ok=True`` result carrying the exit
  status, the produced artifact ids, and the ``code_run`` id (for inspection).
* **AC-2 (negative)** — a session that offers no sandbox seam (``ctx.sandbox is None``)
  → ``ok=False`` (``code_execution_denied``); a **disabled/over-quota** tenant surfaces
  as a ``denied`` run → ``ok=False`` — never a crash.
* **AC-4 (negative, INV-7)** — routed through the governed runner, an *unapproved*
  ``requires_approval`` call is **not executed** (the seam is never touched) and every
  invocation is audited + recorded.
* A sandbox ``timeout`` / ``killed`` / ``failed`` terminal → ``ok=False`` (not a crash);
  empty ``code`` → ``ok=False`` bad args before the seam is touched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

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
from app.domain.entities import AuditEvent, CodeRunStatus, Role
from app.domain.llm import ToolCall
from app.domain.tools import (
    ERROR_APPROVAL_DENIED,
    ERROR_BAD_ARGS,
    RiskTier,
)
from app.services.audit import AuditSink
from app.services.tools import registry
from app.services.tools.impls.run_python import (
    ERROR_CODE_EXECUTION_DENIED,
    ERROR_CODE_EXECUTION_FAILED,
    ERROR_CODE_EXECUTION_TIMEOUT,
    RUN_PYTHON_TOOL_NAME,
    _run_python,
)
from app.services.tools.runner import ToolRunner
from app.services.tools.types import SandboxRun, ToolContext

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


# --- A fake sandbox seam ----------------------------------------------------


class _FakeSandbox:
    """A scripted :class:`SandboxToolRunner`: records the submission, returns a run.

    The real seam (``ChatSandboxToolRunner``) is exercised separately in
    ``test_sandbox_tool_runner.py``; here the tool's own logic is under test, so the
    seam is a fake that returns whatever terminal :class:`SandboxRun` the test scripts.
    """

    def __init__(self, run: SandboxRun) -> None:
        self._run = run
        self.calls: list[dict[str, object]] = []

    async def submit(
        self, *, code: str, packages: tuple[str, ...] = ()
    ) -> SandboxRun:
        self.calls.append({"code": code, "packages": packages})
        return self._run


def _run(
    status: CodeRunStatus,
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    artifact_ids: tuple[uuid.UUID, ...] = (),
) -> SandboxRun:
    return SandboxRun(
        code_run_id=uuid.uuid4(),
        status=status,
        exit_code=exit_code,
        duration_ms=42,
        stdout=stdout,
        stderr=stderr,
        artifact_ids=artifact_ids,
    )


# --- Fixtures ---------------------------------------------------------------


class _World:
    def __init__(self, *, session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id

    @property
    def principal(self) -> Principal:
        return Principal(user_id=self.user_id, tenant_id=self.tenant_id, roles=(Role.MEMBER,))

    def context(self, sandbox: _FakeSandbox | None) -> ToolContext:
        return ToolContext(
            principal=self.principal,
            retrieval=object(),  # type: ignore[arg-type]  # run_python never touches retrieval
            sandbox=sandbox,
        )


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


# ---------------------------------------------------------------------------
# Registration / governance (deny-by-default).
# ---------------------------------------------------------------------------


def test_run_python_is_registered_and_governed_as_highest_tier() -> None:
    defn = registry.get_tool(RUN_PYTHON_TOOL_NAME)
    # The highest tier the tool platform uses: executing arbitrary code is
    # consequential (T2), read/write not read-only.
    assert defn.risk_tier is RiskTier.T2
    assert defn.read_only is False
    # T2 ⇒ requires_approval (INV-7 structural — enforced at construction).
    assert defn.requires_approval is True


def test_run_python_absent_from_default_allowlist() -> None:
    # Deny-by-default + admin-gated: NOT part of the ad-hoc chat default allow-list.
    assert RUN_PYTHON_TOOL_NAME not in registry.default_allowlist()


# ---------------------------------------------------------------------------
# AC-1 — a successful run returns the status + artifact ids + run id.
# ---------------------------------------------------------------------------


async def test_success_returns_status_artifacts_and_run_id(world: _World) -> None:
    artifact_id = uuid.uuid4()
    run = _run(CodeRunStatus.SUCCEEDED, stdout="42\n", artifact_ids=(artifact_id,))
    sandbox = _FakeSandbox(run)
    result = await _run_python({"code": "print(6*7)"}, world.context(sandbox))

    assert result.ok is True
    assert result.payload["runId"] == str(run.code_run_id)
    assert result.payload["status"] == "succeeded"
    assert result.payload["artifactIds"] == [str(artifact_id)]
    # The model-visible reply carries the run id (for GET /code-runs/{id}) + the ids.
    assert str(run.code_run_id) in result.content
    assert str(artifact_id) in result.content
    # The code was submitted to the seam verbatim.
    assert sandbox.calls == [{"code": "print(6*7)", "packages": ()}]


async def test_package_requirements_are_forwarded(world: _World) -> None:
    sandbox = _FakeSandbox(_run(CodeRunStatus.SUCCEEDED))
    await _run_python(
        {"code": "x=1", "packages": ["numpy==2.1.0"]}, world.context(sandbox)
    )
    assert sandbox.calls[0]["packages"] == ("numpy==2.1.0",)


# ---------------------------------------------------------------------------
# AC-2 (negatives) — unavailable / disabled / failing runs are ok=False, not crashes.
# ---------------------------------------------------------------------------


async def test_no_sandbox_seam_is_ok_false_not_crash(world: _World) -> None:
    # A session that does not offer code execution has ctx.sandbox is None.
    result = await _run_python({"code": "print(1)"}, world.context(None))
    assert result.ok is False
    assert result.error == ERROR_CODE_EXECUTION_DENIED


async def test_disabled_or_over_quota_tenant_denied_is_ok_false(world: _World) -> None:
    # The sandbox service refuses a disabled/over-quota tenant BEFORE execution
    # (status=denied); the tool folds that into an ok=False result, never a crash.
    sandbox = _FakeSandbox(_run(CodeRunStatus.DENIED, exit_code=None, stderr="disabled"))
    result = await _run_python({"code": "print(1)"}, world.context(sandbox))
    assert result.ok is False
    assert result.error == ERROR_CODE_EXECUTION_DENIED
    # It still carries the run id so the denial is inspectable.
    assert result.payload["status"] == "denied"


async def test_timeout_terminal_is_ok_false(world: _World) -> None:
    sandbox = _FakeSandbox(_run(CodeRunStatus.TIMEOUT, exit_code=None))
    result = await _run_python({"code": "while True: pass"}, world.context(sandbox))
    assert result.ok is False
    assert result.error == ERROR_CODE_EXECUTION_TIMEOUT


async def test_killed_terminal_is_ok_false(world: _World) -> None:
    sandbox = _FakeSandbox(_run(CodeRunStatus.KILLED, exit_code=None))
    result = await _run_python({"code": "x='a'*10**12"}, world.context(sandbox))
    assert result.ok is False
    assert result.error == ERROR_CODE_EXECUTION_FAILED


async def test_failed_terminal_is_ok_false(world: _World) -> None:
    sandbox = _FakeSandbox(_run(CodeRunStatus.FAILED, exit_code=1, stderr="Traceback…"))
    result = await _run_python({"code": "raise ValueError"}, world.context(sandbox))
    assert result.ok is False
    assert result.error == ERROR_CODE_EXECUTION_FAILED
    # The stderr tail is surfaced so the model can fix the code and re-run.
    assert "Traceback" in result.content


async def test_empty_code_is_ok_false_before_seam(world: _World) -> None:
    sandbox = _FakeSandbox(_run(CodeRunStatus.SUCCEEDED))
    result = await _run_python({"code": "   "}, world.context(sandbox))
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    # The seam was never even touched (fail-closed before submission).
    assert sandbox.calls == []


async def test_missing_code_is_ok_false(world: _World) -> None:
    sandbox = _FakeSandbox(_run(CodeRunStatus.SUCCEEDED))
    result = await _run_python({}, world.context(sandbox))
    assert result.ok is False
    assert result.error == ERROR_BAD_ARGS
    assert sandbox.calls == []


# ---------------------------------------------------------------------------
# AC-4 (INV-7) — through the governed runner: unapproved → NOT executed; audited.
# ---------------------------------------------------------------------------


async def _audit_actions(world: _World) -> list[str]:
    repo = AuditEventRepository(world.session, world.tenant_id)
    events: list[AuditEvent] = await repo.list_recent(limit=50)
    return [e.action for e in events]


def _tool_runner(
    world: _World, *, allowed: frozenset[str], session_id: uuid.UUID | None = None
) -> ToolRunner:
    # No gate passed ⇒ the DenyAllApprovalGate default (fail-closed for T2+).
    return ToolRunner(
        allowed=allowed,
        invocations=ToolInvocationRepository(world.session, world.tenant_id),
        audit=AuditSink(AuditEventRepository(world.session, world.tenant_id)),
        actor=AuditActor.user(world.user_id),
        request_id="req-1",
        source_ip="203.0.113.7",
        session_id=session_id,
    )


async def test_unapproved_run_python_is_not_executed_but_audited(world: _World) -> None:
    """INV-7: a T2 run_python call the default gate denies never touches the seam."""
    sandbox = _FakeSandbox(_run(CodeRunStatus.SUCCEEDED))
    session_id = uuid.uuid4()
    runner = _tool_runner(
        world, allowed=frozenset({RUN_PYTHON_TOOL_NAME}), session_id=session_id
    )
    call = ToolCall(id="c1", name=RUN_PYTHON_TOOL_NAME, arguments={"code": "print(1)"})

    result = await runner.run(
        call=call,
        context=world.context(sandbox),
    )

    assert result.ok is False
    assert result.error == ERROR_APPROVAL_DENIED
    # The handler (and so the seam) never ran — the consequential action was blocked.
    assert sandbox.calls == []
    await world.session.commit()
    # Still audited (INV-6): the denial emits tool.invoked + tool.result.
    actions = await _audit_actions(world)
    assert AuditAction.TOOL_INVOKED.value in actions
    assert AuditAction.TOOL_RESULT.value in actions
    # And a tool_invocations row was recorded for the denied call.
    rows = await ToolInvocationRepository(world.session, world.tenant_id).list_for_session(
        session_id
    )
    assert any(r.tool_name == RUN_PYTHON_TOOL_NAME and not r.ok for r in rows)


async def test_off_allowlist_run_python_is_not_permitted(world: _World) -> None:
    """Deny-by-default: run_python not in the run's allow-list → not_permitted, no exec."""
    sandbox = _FakeSandbox(_run(CodeRunStatus.SUCCEEDED))
    runner = _tool_runner(world, allowed=frozenset())  # empty allow-list
    call = ToolCall(id="c1", name=RUN_PYTHON_TOOL_NAME, arguments={"code": "print(1)"})
    result = await runner.run(call=call, context=world.context(sandbox))
    assert result.ok is False
    assert sandbox.calls == []


async def test_approved_run_python_executes_through_gate(world: _World) -> None:
    """An approved T2 call reaches the seam and returns the run outcome (the positive)."""

    class _AllowGate:
        async def request(self, request: object) -> bool:
            return True

    artifact_id = uuid.uuid4()
    sandbox = _FakeSandbox(
        _run(CodeRunStatus.SUCCEEDED, stdout="ok", artifact_ids=(artifact_id,))
    )
    runner = ToolRunner(
        allowed=frozenset({RUN_PYTHON_TOOL_NAME}),
        invocations=ToolInvocationRepository(world.session, world.tenant_id),
        audit=AuditSink(AuditEventRepository(world.session, world.tenant_id)),
        actor=AuditActor.user(world.user_id),
        request_id="req-1",
        source_ip="203.0.113.7",
        session_id=None,
        gate=_AllowGate(),
    )
    call = ToolCall(id="c1", name=RUN_PYTHON_TOOL_NAME, arguments={"code": "print(1)"})
    result = await runner.run(call=call, context=world.context(sandbox))
    assert result.ok is True
    assert len(sandbox.calls) == 1  # the approved call reached the seam
    assert result.payload["artifactIds"] == [str(artifact_id)]
