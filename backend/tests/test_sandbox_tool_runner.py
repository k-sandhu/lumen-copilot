"""The chat ``run_python`` submission seam — the runtime wiring (issue #231).

Drives :class:`app.sandbox.tool_runner.ChatSandboxToolRunner` **offline** against an
in-memory SQLite database, a **fake object store** (round-trips artifact bytes), a
**fake sandbox runner** (returns a scripted :class:`RunResult` or raises), and an
in-memory backplane that captures the published WS envelopes — so no container engine
/ MinIO / Redis is needed. It proves the seam *around* the merged #230 service:

* a submission **creates + links** a ``code_run`` to the parent chat session + the
  assistant message (the trace), drives the sandbox service to a terminal, and
  returns the :class:`~app.services.tools.types.SandboxRun` summary (exit status +
  artifact ids + run id);
* it **streams** the captured stdout/stderr as ``code_output`` chunks and terminates
  with exactly one ``code_result`` on the same chat stream (the frozen ADR-0013 WS
  contract), correlated by the ``runId``;
* a **disabled tenant** run is refused by the composed service (``status=denied``)
  and comes back as a ``denied`` :class:`SandboxRun` (never an exception) — the tool's
  ``ok=False`` negative;
* a produced file is captured as a tenant-scoped artifact, whose id rides the summary
  and the ``code_result``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.repositories import (
    ChatSessionRepository,
    CodeRunRepository,
    TenantRepository,
    UserRepository,
)
from app.domain.entities import CodeRunStatus, ResourceUsage, Role
from app.realtime.backplane import InMemoryBackplane
from app.sandbox.spec import OutputFile, RunResult, RunSpec
from app.sandbox.tool_runner import ChatSandboxToolRunner
from app.storage.keys import build_artifact_key
from tests._sandbox_helpers import sandbox_settings

import app.db.models  # noqa: F401  isort: skip — register tables on Base.metadata


# --- Fakes ------------------------------------------------------------------


class _FakeStore:
    """No-network stand-in for ``ObjectStore``'s artifact methods (#208 seam)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put_artifact(
        self, tenant_id: str, data: bytes, content_type: str, filename: str
    ) -> object:
        from app.storage.object_store import StoredObject

        key = build_artifact_key(tenant_id, data, filename)
        self.objects[(tenant_id, key)] = data
        return StoredObject(
            key=key, sha256=key.split("/")[2], size_bytes=len(data), content_type=content_type
        )

    async def get_artifact(self, tenant_id: str, key: str) -> bytes:
        return self.objects[(tenant_id, key)]


class _FakeRunner:
    """A scripted sandbox runner: returns a result or raises (never a real container)."""

    def __init__(self, *, result: RunResult | None = None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls = 0

    async def run(self, spec: RunSpec, *, tmpfs_scratch_bytes: int) -> RunResult:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._result is not None
        return self._result


def _ok_result(
    *, stdout: str = "hello", stderr: str = "", output_files: tuple[OutputFile, ...] = ()
) -> RunResult:
    return RunResult(
        status=CodeRunStatus.SUCCEEDED,
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        duration_ms=7,
        output_files=output_files,
        image_digest="sha256:deadbeef",
        resource_usage=ResourceUsage(peak_memory_bytes=2048, output_bytes=len(stdout)),
    )


# --- Fixtures ---------------------------------------------------------------


class _World:
    def __init__(
        self, *, session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.user_id = user_id

    def seam(
        self,
        runner: _FakeRunner,
        *,
        backplane: InMemoryBackplane,
        store: _FakeStore | None = None,
        stream_id: str = "stream-1",
        session_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        settings_overrides: dict[str, object] | None = None,
    ) -> ChatSandboxToolRunner:
        counter = {"seq": 0}

        def _next_seq() -> int:
            s = counter["seq"]
            counter["seq"] += 1
            return s

        return ChatSandboxToolRunner(
            session=self.session,
            tenant_id=self.tenant_id,
            owner_id=self.user_id,
            runner=runner,  # type: ignore[arg-type]  # structural fake
            object_store=(store or _FakeStore()),  # type: ignore[arg-type]  # structural fake
            settings=sandbox_settings(**(settings_overrides or {})),
            backplane=backplane,
            stream_id=stream_id,
            request_id="req-test",
            source_ip="203.0.113.5",
            next_seq=_next_seq,
            session_id=session_id,
            message_id=message_id,
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


def _events(backplane: InMemoryBackplane, stream_id: str) -> list[dict[str, Any]]:
    # The InMemoryBackplane buffers every published envelope for replay.
    return list(backplane._replay.get(stream_id, []))  # noqa: SLF001 — test inspection


# ---------------------------------------------------------------------------
# AC-1 — submit runs to a terminal, links the run, returns the summary.
# ---------------------------------------------------------------------------


async def test_submit_runs_captures_and_returns_summary(world: _World) -> None:
    backplane = InMemoryBackplane()
    output = OutputFile(filename="chart.png", content_type="image/png", data=b"\x89PNGdata")
    runner = _FakeRunner(result=_ok_result(stdout="done\n", output_files=(output,)))
    message_id = uuid.uuid4()
    # A real chat session so the code_run's session_id FK resolves.
    chat = await ChatSessionRepository(world.session, world.tenant_id).create(
        owner_id=world.user_id, model="m", title="t"
    )
    seam = world.seam(
        runner, backplane=backplane, session_id=chat.id, message_id=message_id
    )

    summary = await seam.submit(code="print('done')")

    assert summary.status is CodeRunStatus.SUCCEEDED
    assert summary.exit_code == 0
    assert summary.stdout == "done\n"
    assert len(summary.artifact_ids) == 1
    # The code_run was persisted, linked to the parent session + the trace (message).
    run = await CodeRunRepository(world.session, world.tenant_id).get(summary.code_run_id)
    assert run is not None
    assert run.session_id == chat.id
    assert run.trace_id == message_id
    assert run.owner_id == world.user_id  # runs on the streaming user's behalf (INV-2)
    assert [str(a) for a in run.artifact_ids] == [str(a) for a in summary.artifact_ids]


# ---------------------------------------------------------------------------
# WS contract — code_output chunks then exactly one code_result (ADR-0013).
# ---------------------------------------------------------------------------


async def test_streams_code_output_then_one_code_result(world: _World) -> None:
    backplane = InMemoryBackplane()
    runner = _FakeRunner(result=_ok_result(stdout="line1\n", stderr="warn\n"))
    seam = world.seam(runner, backplane=backplane, stream_id="s-1")

    summary = await seam.submit(code="x=1")

    events = _events(backplane, "s-1")
    outputs = [e for e in events if e.get("name") == "code_output"]
    results = [e for e in events if e.get("name") == "code_result"]
    # Both captured streams surfaced as code_output chunks, correlated by runId.
    assert {e["data"]["stream"] for e in outputs} == {"stdout", "stderr"}
    assert all(e["data"]["runId"] == str(summary.code_run_id) for e in outputs)
    # Exactly ONE terminal code_result, after the chunks, carrying the outcome.
    assert len(results) == 1
    result_event = results[0]
    assert result_event["data"]["runId"] == str(summary.code_run_id)
    assert result_event["data"]["status"] == "succeeded"
    assert result_event["data"]["artifactIds"] == []
    # Monotonic seq: the code_result is the last-emitted of the run's events.
    assert result_event["seq"] == max(e["seq"] for e in outputs + results)


# ---------------------------------------------------------------------------
# AC-2 (negative) — a disabled tenant is denied by the composed service.
# ---------------------------------------------------------------------------


async def test_disabled_tenant_is_denied_without_calling_runner(world: _World) -> None:
    backplane = InMemoryBackplane()
    runner = _FakeRunner(result=_ok_result())
    seam = world.seam(
        runner, backplane=backplane, settings_overrides={"SANDBOX_ENABLED": "false"}
    )

    summary = await seam.submit(code="print(1)")

    # Refused before execution: the runner was NEVER called, the run is denied.
    assert summary.status is CodeRunStatus.DENIED
    assert runner.calls == 0
    # A denied run emits a code_result (terminal) but no process code_output.
    events = _events(backplane, "stream-1")
    assert [e for e in events if e.get("name") == "code_output"] == []
    results = [e for e in events if e.get("name") == "code_result"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "denied"


async def test_runner_crash_becomes_failed_terminal_not_exception(world: _World) -> None:
    backplane = InMemoryBackplane()
    runner = _FakeRunner(raises=RuntimeError("runner exploded"))
    seam = world.seam(runner, backplane=backplane)

    # The composed service catches the crash and writes ``failed`` — the seam never
    # raises, so the tool always gets a terminal record back.
    summary = await seam.submit(code="boom")

    assert summary.status is CodeRunStatus.FAILED
    results = [e for e in _events(backplane, "stream-1") if e.get("name") == "code_result"]
    assert len(results) == 1
    assert results[0]["data"]["status"] == "failed"
