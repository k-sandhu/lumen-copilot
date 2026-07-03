"""The chat-facing ``run_python`` submission seam (issue #231).

Makes the merged sandbox engine (#230) drivable from the agent tool loop. The
``run_python`` tool (``app.services.tools.impls.run_python``) never talks to the
sandbox internals directly — it holds only the narrow
:class:`~app.services.tools.types.SandboxToolRunner` Protocol. This module is the
concrete implementation the chat runtime wires into the tool context:

* it **creates** the ``queued`` ``code_run`` linked to the parent chat session +
  assistant message (``session_id`` / the trace), through the tenant-scoped
  :class:`~app.db.repositories.CodeRunRepository` (the only SQL);
* it **submits** the run to the merged :class:`~app.sandbox.service.SandboxService`
  — the owning module for code execution (ADR-0013 §1). A disabled/over-quota
  tenant is refused there (``status=denied``, audited) *before* any container
  launches, so admin-gating and the quota gate hold by delegation — the tool never
  re-implements them;
* it **streams** the captured output over the chat stream as ``code_output`` chunks
  and terminates with exactly one ``code_result`` (the frozen ADR-0013 WS contract),
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
seam re-reads that terminal record and replays its (output-size-capped, G7) stdout
/stderr as ``code_output`` chunks *after* the terminal is known — the contract
permits zero-or-more chunks in ``seq`` order before the single ``code_result``, and
a truncating run still terminates with ``code_result``. Per-token live streaming is
the inspector's concern (#232) and layers on top without changing this shape.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repositories import CodeRunRepository
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
    runtime's own DB session, the tenant/owner (from the streaming principal, never
    tool args), the injected :class:`SandboxRunner` (live HTTP client or a test
    fake), the object store, the backplane the answer streams over, and the
    ``stream_id`` / ``session_id`` / ``message_id`` the run links back to. All
    tenant/owner scoping + the deny-by-default quota/enable gate live in the
    composed :class:`SandboxService`; this class only creates the run row, drives the
    service, and renders the WS events.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
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
        self._session = session
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

    async def submit(self, *, code: str, timeout_s: int | None = None) -> SandboxRun:
        """Run ``code`` to a terminal status; stream its output; return the summary.

        Creates the ``queued`` ``code_run`` (linked to the parent session + the
        assistant message via ``trace_id``), submits it to the sandbox service, then
        re-reads the terminal record and emits its captured stdout/stderr as
        ``code_output`` chunks followed by exactly one ``code_result``. ``timeout_s``
        is accepted for the seam's contract and recorded for the run; the hard
        wall-clock cap remains the configured ceiling (G6) — a per-call hint can
        never widen it, so it is not honoured as an override here.
        """
        code_runs = CodeRunRepository(self._session, self._tenant_id)
        run = await code_runs.create(
            owner_id=self._owner_id,
            code=code,
            session_id=self._session_id,
            # Link the code-run to the parent chat/agent trace: the assistant message
            # id doubles as the trace id until the agent-trace tables land (mirrors
            # the code_runs row's documented ``trace_id`` semantics, ADR-0013 §4).
            trace_id=self._message_id,
        )
        run_id = run.id

        service = SandboxService(
            tenant_id=self._tenant_id,
            owner_id=self._owner_id,
            runner=self._runner,
            object_store=self._store,
            settings=self._settings,
            request_id=self._request_id,
            source_ip=self._source_ip,
        )
        # The service walks the row through the deny-by-default gate → running →
        # terminal, capturing stdout/stderr/exit/artifacts and auditing every
        # transition. It never raises for a run concern (a crash inside is written as
        # ``failed``), so the tool always gets a terminal record back.
        await service.execute(self._session, run_id)

        terminal = await code_runs.get(run_id)
        if terminal is None:  # pragma: no cover — the run was just created in this tx
            log.error("sandbox.tool.run_vanished", code_run_id=str(run_id))
            summary = SandboxRun(
                code_run_id=run_id,
                status=CodeRunStatus.FAILED,
                exit_code=None,
                duration_ms=None,
                stdout="",
                stderr="",
            )
            await self._emit_result(summary)
            return summary

        await self._stream_output(terminal)
        summary = _to_summary(terminal)
        await self._emit_result(summary)
        return summary

    async def _stream_output(self, run: CodeRun) -> None:
        """Emit the run's captured stdout/stderr as ``code_output`` chunks (ADR-0013).

        The engine captures the whole (output-size-capped, G7) output; this replays it
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
        """Emit the single terminal ``code_result`` for the run (ADR-0013 contract).

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
                },
            ),
        )


def _to_summary(run: CodeRun) -> SandboxRun:
    """Project a terminal ``code_run`` record into the tool-facing summary."""
    return SandboxRun(
        code_run_id=run.id,
        status=run.status,
        exit_code=run.exit_code,
        duration_ms=run.duration_ms,
        stdout=run.stdout,
        stderr=run.stderr,
        artifact_ids=tuple(run.artifact_ids),
    )


__all__ = ["ChatSandboxToolRunner"]
