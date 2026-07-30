"""Reusable sandbox lifecycle/execution service tests (ADR-0020, issue #457)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.errors import ConflictError, NotFoundError
from app.db.base import Base
from app.db.repositories import (
    AuditEventRepository,
    ChatSessionRepository,
    CodeRunRepository,
    SandboxSessionRepository,
    TenantRepository,
    TenantSandboxPolicyRepository,
    UserRepository,
)
from app.domain.entities import CodeRunStatus, ResourceUsage, Role, SandboxSessionStatus
from app.sandbox.service import SandboxReadService, SandboxService, SandboxSessionService
from app.sandbox.spec import OutputFile, RunResult, RunSpec, SandboxSessionSpec
from app.storage.keys import build_artifact_key
from tests._sandbox_helpers import sandbox_settings

import app.db.models  # noqa: F401  isort: skip


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put_artifact(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> object:
        from app.storage.object_store import StoredObject

        key = build_artifact_key(tenant_id, data, filename)
        self.objects[(tenant_id, key)] = data
        return StoredObject(
            key=key,
            sha256=key.split("/")[2],
            size_bytes=len(data),
            content_type=content_type,
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        return self.objects[(tenant_id, key)]


class _FakeRunner:
    def __init__(
        self,
        *,
        result: RunResult | None = None,
        raises: Exception | None = None,
        ensure_raises: Exception | None = None,
    ) -> None:
        self.result = result or RunResult(
            status=CodeRunStatus.SUCCEEDED,
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=12,
            image_digest="python@sha256:runtime",
            resource_usage=ResourceUsage(output_bytes=2),
        )
        self.raises = raises
        self.ensure_raises = ensure_raises
        self.ensured: list[SandboxSessionSpec] = []
        self.executions: list[tuple[SandboxSessionSpec, RunSpec]] = []
        self.resets: list[tuple[SandboxSessionSpec, SandboxSessionSpec]] = []
        self.closed: list[SandboxSessionSpec] = []
        self.cancelled: list[tuple[SandboxSessionSpec, UUID]] = []

    async def ensure_session(self, value: SandboxSessionSpec) -> None:
        self.ensured.append(value)
        if self.ensure_raises is not None:
            raise self.ensure_raises

    async def execute(self, value: SandboxSessionSpec, run: RunSpec) -> RunResult:
        self.executions.append((value, run))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def reset_session(
        self, previous: SandboxSessionSpec, replacement: SandboxSessionSpec
    ) -> None:
        self.resets.append((previous, replacement))

    async def close_session(self, value: SandboxSessionSpec) -> None:
        self.closed.append(value)

    async def cancel(self, value: SandboxSessionSpec, execution_id: UUID) -> None:
        self.cancelled.append((value, execution_id))


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as value:
            yield value
    finally:
        await engine.dispose()


async def _principal_chat(
    session: AsyncSession, *, name: str = "Acme", email: str = "alice@acme.test"
) -> tuple[UUID, UUID, UUID]:
    tenant = await TenantRepository(session).create(name=name)
    owner = await UserRepository(session, tenant.id).create(
        email=email, password_hash="h", roles=[Role.MEMBER]
    )
    chat = await ChatSessionRepository(session, tenant.id).create(
        owner_id=owner.id, model="openrouter:test/model"
    )
    return tenant.id, owner.id, chat.id


async def _enable(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    allowed: tuple[str, ...] = (),
    denied: tuple[str, ...] = (),
) -> None:
    await TenantSandboxPolicyRepository(session, tenant_id).upsert(
        enabled=True,
        allowed_packages=allowed,
        denied_packages=denied,
        egress_allowed=False,
        egress_allowlist=(),
        max_runtime_s=30,
        max_memory_mb=512,
        daily_runtime_cap_s=3600,
        max_concurrency=2,
        updated_by=None,
    )


def _service(tenant: UUID, owner: UUID, runner: _FakeRunner, store: _FakeStore) -> SandboxService:
    return SandboxService(
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        object_store=store,  # type: ignore[arg-type]
        settings=sandbox_settings(),
    )


async def _run(
    session: AsyncSession,
    tenant: UUID,
    owner: UUID,
    chat: UUID,
    *,
    code: str = "print('ok')",
    packages: tuple[str, ...] = (),
):
    return await CodeRunRepository(session, tenant).create(
        owner_id=owner,
        session_id=chat,
        code=code,
        requested_packages=packages,
    )


async def _actions(session: AsyncSession, tenant: UUID) -> list[str]:
    return [value.action for value in await AuditEventRepository(session, tenant).list_recent()]


async def test_repeated_runs_reuse_same_chat_session_and_generation(
    session: AsyncSession,
) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner, store = _FakeRunner(), _FakeStore()
    service = _service(tenant, owner, runner, store)

    first = await _run(session, tenant, owner, chat, code="open('/workspace/a','w').write('x')")
    second = await _run(session, tenant, owner, chat, code="print(open('/workspace/a').read())")
    assert await service.execute(session, first.id) is CodeRunStatus.SUCCEEDED
    assert await service.execute(session, second.id) is CodeRunStatus.SUCCEEDED

    persisted_first = await CodeRunRepository(session, tenant).get(first.id)
    persisted_second = await CodeRunRepository(session, tenant).get(second.id)
    assert persisted_first is not None and persisted_second is not None
    assert persisted_first.sandbox_session_id == persisted_second.sandbox_session_id
    assert persisted_first.sandbox_generation == persisted_second.sandbox_generation == 1
    assert [value.sandbox_session_id for value, _ in runner.executions] == [
        persisted_first.sandbox_session_id,
        persisted_first.sandbox_session_id,
    ]
    assert (await _actions(session, tenant)).count("sandbox_session.created") == 1


async def test_different_chats_receive_different_sandboxes(session: AsyncSession) -> None:
    tenant, owner, first_chat = await _principal_chat(session)
    second_chat = await ChatSessionRepository(session, tenant).create(
        owner_id=owner, model="openrouter:test/model"
    )
    await _enable(session, tenant)
    runner, store = _FakeRunner(), _FakeStore()
    service = _service(tenant, owner, runner, store)

    for chat in (first_chat, second_chat.id):
        value = await _run(session, tenant, owner, chat)
        await service.execute(session, value.id)

    assert runner.executions[0][0].sandbox_session_id != runner.executions[1][0].sandbox_session_id


async def test_allowed_packages_reach_runner_canonicalized(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant, allowed=("numpy", "pandas"))
    runner, store = _FakeRunner(), _FakeStore()
    value = await _run(
        session,
        tenant,
        owner,
        chat,
        packages=("NumPy==2.1.0", "pandas[excel]>=2"),
    )

    assert (
        await _service(tenant, owner, runner, store).execute(session, value.id)
        is CodeRunStatus.SUCCEEDED
    )
    assert runner.executions[0][1].packages == ("numpy==2.1.0", "pandas[excel]>=2")


async def test_preinstalled_package_runs_on_an_empty_allow_list_without_an_install(
    session: AsyncSession,
) -> None:
    """Issue #504: what the execution image ships needs no install, so no grant.

    Every tenant starts with ``allowed_packages=()`` (deny-by-default, #233) and the
    sandbox never has a network, so before this a plain ``packages=["pandas"]`` was
    DENIED with no install path anyone could offer. pandas is baked into
    ``lumen-sandbox-exec``; the run is admitted and the runner is asked to install
    NOTHING — which is what makes it work on a deploy with no route to PyPI.
    """
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant, allowed=())
    runner, store = _FakeRunner(), _FakeStore()
    value = await _run(session, tenant, owner, chat, packages=("pandas", "matplotlib>=3"))

    assert (
        await _service(tenant, owner, runner, store).execute(session, value.id)
        is CodeRunStatus.SUCCEEDED
    )
    assert runner.executions[0][1].packages == ()


async def test_package_outside_the_image_and_allow_list_is_denied(
    session: AsyncSession,
) -> None:
    """The negative half of #504: a real install still needs a real grant.

    Admitting the baked stack must not turn into admitting everything — an unknown
    distribution is refused before any container command runs, and the refusal says
    what to change.
    """
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant, allowed=())
    runner, store = _FakeRunner(), _FakeStore()
    value = await _run(session, tenant, owner, chat, packages=("scikit-learn",))

    assert (
        await _service(tenant, owner, runner, store).execute(session, value.id)
        is CodeRunStatus.DENIED
    )
    assert runner.ensured == []
    persisted = await CodeRunRepository(session, tenant).get(value.id)
    assert persisted is not None
    assert "not installed in the code sandbox image" in persisted.stderr
    assert "allowed-packages" in persisted.stderr


async def test_denied_package_never_reaches_runner(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant, allowed=("*",), denied=("requests",))
    runner, store = _FakeRunner(), _FakeStore()
    value = await _run(session, tenant, owner, chat, packages=("requests==2.32.3",))

    assert (
        await _service(tenant, owner, runner, store).execute(session, value.id)
        is CodeRunStatus.DENIED
    )
    assert runner.ensured == []
    assert runner.executions == []
    assert "code_run.denied" in await _actions(session, tenant)


async def test_runner_failure_is_terminal_not_stuck(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner = _FakeRunner(raises=RuntimeError("boom"))
    value = await _run(session, tenant, owner, chat)

    status = await _service(tenant, owner, runner, _FakeStore()).execute(session, value.id)
    persisted = await CodeRunRepository(session, tenant).get(value.id)
    assert status is CodeRunStatus.FAILED
    assert persisted is not None and persisted.status is CodeRunStatus.FAILED


async def test_session_start_failure_is_terminal_not_stuck(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner = _FakeRunner(ensure_raises=RuntimeError("runner unavailable"))
    value = await _run(session, tenant, owner, chat)

    status = await _service(tenant, owner, runner, _FakeStore()).execute(session, value.id)
    persisted = await CodeRunRepository(session, tenant).get(value.id)

    assert status is CodeRunStatus.FAILED
    assert persisted is not None and persisted.status is CodeRunStatus.FAILED
    sandbox = await SandboxSessionRepository(session, tenant).get_for_chat(chat)
    assert sandbox is not None and sandbox.status is SandboxSessionStatus.ERROR
    assert persisted.finished_at is not None


async def test_output_files_become_tenant_scoped_artifacts(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    result = RunResult(
        status=CodeRunStatus.SUCCEEDED,
        exit_code=0,
        stdout="made it",
        stderr="",
        duration_ms=5,
        output_files=(OutputFile("report.csv", "text/csv", b"a,b\n1,2\n"),),
    )
    runner, store = _FakeRunner(result=result), _FakeStore()
    value = await _run(session, tenant, owner, chat)
    await _service(tenant, owner, runner, store).execute(session, value.id)

    persisted = await CodeRunRepository(session, tenant).get(value.id)
    assert persisted is not None and len(persisted.artifact_ids) == 1
    assert list(store.objects.values()) == [b"a,b\n1,2\n"]


async def test_duplicate_delivery_does_not_execute_twice(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner, store = _FakeRunner(), _FakeStore()
    value = await _run(session, tenant, owner, chat)
    service = _service(tenant, owner, runner, store)

    await service.execute(session, value.id)
    await service.execute(session, value.id)
    assert len(runner.executions) == 1


async def test_cancel_winning_completion_race_keeps_killed_terminal_state(
    session: AsyncSession,
) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    value = await _run(session, tenant, owner, chat)

    class _CancelWinsRunner(_FakeRunner):
        async def execute(self, sandbox: SandboxSessionSpec, run: RunSpec) -> RunResult:
            await CodeRunRepository(session, tenant).mark_terminal(
                value.id,
                status=CodeRunStatus.KILLED,
                finished_at=datetime.now(UTC),
                stderr="Code execution was cancelled.",
            )
            return await super().execute(sandbox, run)

    runner = _CancelWinsRunner()
    status = await _service(tenant, owner, runner, _FakeStore()).execute(session, value.id)
    persisted = await CodeRunRepository(session, tenant).get(value.id)

    assert status is CodeRunStatus.KILLED
    assert persisted is not None
    assert persisted.status is CodeRunStatus.KILLED
    assert persisted.stderr == "Code execution was cancelled."


async def test_cancel_before_running_transition_never_reaches_execution(
    session: AsyncSession,
) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    value = await _run(session, tenant, owner, chat)

    class _CancelDuringEnsureRunner(_FakeRunner):
        async def ensure_session(self, sandbox: SandboxSessionSpec) -> None:
            await super().ensure_session(sandbox)
            await CodeRunRepository(session, tenant).mark_terminal(
                value.id,
                status=CodeRunStatus.KILLED,
                finished_at=datetime.now(UTC),
                stderr="Code execution was cancelled.",
            )

    runner = _CancelDuringEnsureRunner()
    status = await _service(tenant, owner, runner, _FakeStore()).execute(session, value.id)

    assert status is CodeRunStatus.KILLED
    assert runner.executions == []


async def test_reset_and_close_drive_runner_and_audit(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner = _FakeRunner()
    lifecycle = SandboxSessionService(
        session,
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        settings=sandbox_settings(),
    )
    first = await lifecycle.ensure(chat)
    second = await lifecycle.reset(chat)
    await lifecycle.close(chat)

    assert first.id == second.id
    assert second.generation == 2
    assert runner.resets[0][0].generation == 1
    assert runner.resets[0][1].generation == 2
    assert runner.closed[-1].generation == 2
    stored = await SandboxSessionRepository(session, tenant).get(first.id)
    assert stored is not None and stored.status is SandboxSessionStatus.CLOSED
    actions = await _actions(session, tenant)
    assert "sandbox_session.created" in actions
    assert "sandbox_session.reset" in actions
    assert "sandbox_session.closed" in actions


async def test_cancel_kills_run_and_advances_generation(session: AsyncSession) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner = _FakeRunner()
    lifecycle = SandboxSessionService(
        session,
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        settings=sandbox_settings(),
    )
    sandbox = await lifecycle.ensure(chat)
    value = await _run(session, tenant, owner, chat)
    await CodeRunRepository(session, tenant).mark_running(
        value.id,
        started_at=datetime.now(UTC),
        sandbox_session_id=sandbox.id,
        sandbox_generation=sandbox.generation,
    )

    cancelled = await lifecycle.cancel(value.id)
    advanced = await SandboxSessionRepository(session, tenant).get(sandbox.id)
    assert cancelled.status is CodeRunStatus.KILLED
    assert len(runner.cancelled) == 1
    assert runner.cancelled[0][0].sandbox_session_id == sandbox.id
    assert runner.cancelled[0][1] == value.id
    assert advanced is not None and advanced.generation == 2
    assert "code_run.cancelled" in await _actions(session, tenant)
    with pytest.raises(ConflictError):
        await lifecycle.cancel(value.id)


async def test_ensured_session_carries_the_configured_output_budget(
    session: AsyncSession,
) -> None:
    """``SANDBOX_OUTPUT_BYTES_CAP`` reaches the runner instead of being dropped.

    The runner reads a run's output directory into ITS OWN memory and is the single
    holder of the Docker socket, so an unbounded collection let one chat turn OOM the
    process every tenant's code execution depends on. The setting existed but nothing
    populated the spec field, so the number never left the backend.
    """
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner = _FakeRunner()
    settings = sandbox_settings(SANDBOX_OUTPUT_BYTES_CAP="2097152")
    lifecycle = SandboxSessionService(
        session,
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        settings=settings,
    )

    await lifecycle.ensure(chat)

    assert runner.ensured[0].output_bytes_cap == 2 * 1024 * 1024
    assert settings.sandbox_output_bytes_cap == 2 * 1024 * 1024


async def test_cancelling_old_run_does_not_destroy_or_advance_new_generation(
    session: AsyncSession,
) -> None:
    tenant, owner, chat = await _principal_chat(session)
    await _enable(session, tenant)
    runner = _FakeRunner()
    lifecycle = SandboxSessionService(
        session,
        tenant_id=tenant,
        owner_id=owner,
        runner=runner,
        settings=sandbox_settings(),
    )
    first = await lifecycle.ensure(chat)
    value = await _run(session, tenant, owner, chat)
    await CodeRunRepository(session, tenant).mark_running(
        value.id,
        started_at=datetime.now(UTC),
        sandbox_session_id=first.id,
        sandbox_generation=first.generation,
    )
    second = await lifecycle.reset(chat)

    cancelled = await lifecycle.cancel(value.id)
    current = await SandboxSessionRepository(session, tenant).get(first.id)

    assert cancelled.status is CodeRunStatus.KILLED
    assert runner.cancelled[-1][0].generation == 1
    assert current is not None
    assert current.generation == second.generation == 2


async def test_cross_tenant_and_non_owner_reads_are_404(session: AsyncSession) -> None:
    tenant_a, owner_a, chat_a = await _principal_chat(session)
    tenant_b, owner_b, _ = await _principal_chat(session, name="Globex", email="bob@globex.test")
    run = await _run(session, tenant_a, owner_a, chat_a)

    with pytest.raises(NotFoundError):
        await SandboxReadService(session, tenant_id=tenant_b, owner_id=owner_b).get(run.id)

    other = await UserRepository(session, tenant_a).create(
        email="mallory@acme.test", password_hash="h", roles=[Role.MEMBER]
    )
    with pytest.raises(NotFoundError):
        await SandboxReadService(session, tenant_id=tenant_a, owner_id=other.id).get(run.id)
