"""The chat-facing ``run_python`` submission seam (issue #231).

Makes the merged sandbox engine (#230) drivable from the agent tool loop. The
``run_python`` tool (``app.services.tools.impls.run_python``) never talks to the
sandbox internals directly — it holds only the narrow
:class:`~app.services.tools.types.SandboxToolRunner` Protocol. This module is the
concrete implementation the chat runtime wires into the tool context:

* it **creates** the ``queued`` ``code_run`` linked to the parent chat session +
  assistant message (``session_id`` / the trace), in its own short tenant-scoped
  transaction through the
  :class:`~app.db.repositories.CodeRunRepository` (the only SQL);
* it **submits** the run to the merged :class:`~app.sandbox.service.SandboxService`
  — the owning module for code execution (ADR-0020 §1). A disabled tenant or
  disallowed package request is refused there (``status=denied``, audited) *before*
  tenant code launches, so policy gating holds by delegation — the tool never
  re-implements it;
* it **streams** the captured output over the chat stream as ``code_output`` chunks
  and terminates with exactly one ``code_result`` (the ADR-0020 WS contract),
  correlated by the ``runId`` (the ``code_run`` id);
* it returns the terminal :class:`~app.services.tools.types.SandboxRun` domain summary
  to the tool — never a runner/Docker wire object (ADR-0004 boundary rule).

**Offline-safe by construction.** The service + runner are injected: the chat
runtime builds this with the live :class:`~app.sandbox.runner.HttpSandboxRunner`
(the worker's only container-engine surface), while the offline tool tests inject a
:class:`~app.sandbox.runner.SandboxRunner` fake. The whole submission lifecycle is
exercised without a container engine; the true kernel-level isolation is #230's
live-gated coverage.

The engine drives a run to a terminal status and captures the whole result; this
seam re-reads that terminal record and replays its captured stdout
/stderr as ``code_output`` chunks *after* the terminal is known — the contract
permits zero-or-more chunks in ``seq`` order before the single ``code_result``, and
a truncating run still terminates with ``code_result``. Per-token live streaming is
the inspector's concern (#232) and layers on top without changing this shape.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repositories import CodeRunRepository
from app.db.tenant_context import bind_tenant
from app.domain.entities import CodeRun, CodeRunStatus
from app.realtime import envelopes
from app.realtime.backplane import Backplane
from app.sandbox.runner import SandboxRunner
from app.sandbox.service import SandboxService
from app.services.tools.types import SandboxRun
from app.storage import ObjectStore

log = get_logger(__name__)

# The terminal-but-never-launched status: no run record output to replay as chunks.
_NO_OUTPUT_STATUSES = frozenset({CodeRunStatus.DENIED})


class ChatSandboxToolRunner:
    """Submit a ``run_python`` code run and stream its output over the chat stream.

    Constructed per answer by the chat runtime with the run's collaborators — the
    runtime's DB session factory, the tenant/owner (from the streaming principal,
    never tool args), the injected :class:`SandboxRunner` (live HTTP client or a test
    fake), the object store, the backplane the answer streams over, and the
    ``stream_id`` / ``session_id`` / ``message_id`` the run links back to. The
    dedicated session keeps code execution out of the answer transaction: the queued
    row is committed before execution and the running row is committed before the
    unbounded runner call, so cancellation can observe it. All tenant/owner scoping +
    the deny-by-default enable/package gate live in the composed
    :class:`SandboxService`; this class creates the run row, drives the service, and
    renders the WS events.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        tenant_id: UUID,
        owner_id: UUID,
        runner: SandboxRunner,
        object_store: ObjectStore,
        settings: Settings,
        backplane: Backplane,
        stream_id: str,
        request_id: str,
        source_ip: str,
        next_seq: Callable[[], int],
        session_id: UUID | None = None,
        message_id: UUID | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._runner = runner
        self._store = object_store
        self._settings = settings
        self._backplane = backplane
        self._stream_id = stream_id
        self._request_id = request_id
        self._source_ip = source_ip
        # The stream's shared monotonic seq allocator (the chat runtime owns the
        # counter; ``code_output``/``code_result`` draw from it so they interleave
        # correctly with the answer's ``delta``/tool events, issue #37).
        self._next_seq = next_seq
        self._session_id = session_id
        self._message_id = message_id

    async def submit(self, *, code: str, packages: tuple[str, ...] = ()) -> SandboxRun:
        """Run ``code`` to a terminal status; stream its output; return the summary.

        Creates the ``queued`` ``code_run`` (linked to the parent session + the
        assistant message via ``trace_id``), submits it to the sandbox service, then
        re-reads the terminal record and emits its captured stdout/stderr as
        ``code_output`` chunks followed by exactly one ``code_result``. Package
        requirements are policy-checked and installed by the runner before
        tenant inputs are staged. There is no automatic execution timeout.
        """
        async with self._sessionmaker() as session:
            try:
                await bind_tenant(session, self._tenant_id)
                code_runs = CodeRunRepository(session, self._tenant_id)
                run = await code_runs.create(
                    owner_id=self._owner_id,
                    code=code,
                    session_id=self._session_id,
                    # Link the code-run to the parent chat/agent trace: the assistant
                    # message id doubles as the trace id until the agent-trace tables
                    # land (mirrors the row's documented ``trace_id`` semantics).
                    trace_id=self._message_id,
                    requested_packages=packages,
                )
                run_id = run.id
                # Cancellation/reset requests use independent transactions. Make the
                # queued identity visible before execution, then re-arm the transaction-
                # local RLS GUC for the execution phase.
                await session.commit()
                await bind_tenant(session, self._tenant_id)

                service = SandboxService(
                    tenant_id=self._tenant_id,
                    owner_id=self._owner_id,
                    runner=self._runner,
                    object_store=self._store,
                    settings=self._settings,
                    request_id=self._request_id,
                    source_ip=self._source_ip,
                )
                # The service walks the row through the deny-by-default gate → running
                # → terminal, capturing stdout/stderr/exit/artifacts and auditing every
                # transition. It commits running state before the unbounded runner call
                # and never raises for a run concern (a crash becomes ``failed``).
                #
                # ``execute_outcome`` (not ``execute``) because the typed
                # ``reason_code`` is NOT a column on ``code_runs`` — only the refusal's
                # prose is, as ``stderr``. Reading it from the outcome is what lets the
                # tool put the stable code in the ``tool_invocations`` row instead of a
                # truncated sentence (issue #502).
                outcome = await service.execute_outcome(session, run_id)
                terminal = await code_runs.get(run_id)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        if terminal is None:  # pragma: no cover — durable row vanished unexpectedly
            log.error("sandbox.tool.run_vanished", code_run_id=str(run_id))
            summary = SandboxRun(
                code_run_id=run_id,
                status=CodeRunStatus.FAILED,
                exit_code=None,
                duration_ms=None,
                stdout="",
                stderr="",
                sandbox_session_id=None,
                sandbox_generation=None,
                reason_code=outcome.reason_code,
            )
            await self._emit_result(summary)
            return summary

        await self._stream_output(terminal)
        summary = _to_summary(terminal, reason_code=outcome.reason_code)
        await self._emit_result(summary)
        return summary

    async def _stream_output(self, run: CodeRun) -> None:
        """Emit the run's captured stdout/stderr as ``code_output`` chunks (ADR-0020).

        The engine captures the whole output; this replays it
        as at most one stdout chunk and one stderr chunk in ``seq`` order before the
        terminal ``code_result``. A run refused before execution (``denied``) has no
        process output, so no chunk is emitted.
        """
        if run.status in _NO_OUTPUT_STATUSES:
            return
        if run.stdout:
            await self._emit_output(run.id, stream="stdout", text=run.stdout)
        if run.stderr:
            await self._emit_output(run.id, stream="stderr", text=run.stderr)

    async def _emit_output(self, run_id: UUID, *, stream: str, text: str) -> None:
        await self._backplane.publish(
            self._stream_id,
            envelopes.event(
                self._stream_id,
                self._next_seq(),
                name="code_output",
                data={"runId": str(run_id), "stream": stream, "text": text},
            ),
        )

    async def _emit_result(self, run: SandboxRun) -> None:
        """Emit the single terminal ``code_result`` for the run (ADR-0020 contract).

        This is a run-level terminal, NOT the chat stream's terminal — the chat
        stream still ends with its own ``done``/``error``. ``artifactIds`` are the
        ids of any files the run produced (collected via CC-B #208).
        """
        await self._backplane.publish(
            self._stream_id,
            envelopes.event(
                self._stream_id,
                self._next_seq(),
                name="code_result",
                data={
                    "runId": str(run.code_run_id),
                    "status": run.status.value,
                    "exitCode": run.exit_code,
                    "durationMs": run.duration_ms,
                    "artifactIds": [str(a) for a in run.artifact_ids],
                    "sandboxSessionId": (
                        str(run.sandbox_session_id) if run.sandbox_session_id else None
                    ),
                    "sandboxGeneration": run.sandbox_generation,
                },
            ),
        )


def _to_summary(run: CodeRun, *, reason_code: str | None = None) -> SandboxRun:
    """Project a terminal ``code_run`` record into the tool-facing summary.

    ``reason_code`` rides alongside the row rather than out of it: the durable row
    stores only the refusal's model-facing prose, while the typed code comes from
    the execution outcome (issue #502).
    """
    return SandboxRun(
        code_run_id=run.id,
        status=run.status,
        exit_code=run.exit_code,
        duration_ms=run.duration_ms,
        stdout=run.stdout,
        stderr=run.stderr,
        artifact_ids=tuple(run.artifact_ids),
        sandbox_session_id=run.sandbox_session_id,
        sandbox_generation=run.sandbox_generation,
        reason_code=reason_code,
    )


__all__ = ["ChatSandboxToolRunner"]
