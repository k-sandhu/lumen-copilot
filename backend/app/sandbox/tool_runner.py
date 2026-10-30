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

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.db import session as db_session
from app.db.repositories import AuditEventRepository, CodeRunRepository, SandboxSessionRepository
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.code_execution import (
    SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
    sandbox_reason_public_message,
)
from app.domain.entities import AuditOutcome, CodeRun, CodeRunStatus
from app.realtime import envelopes
from app.realtime.backplane import Backplane
from app.sandbox.accounting import SandboxAccounting
from app.sandbox.admission import AdmissionOwnership
from app.sandbox.runner import SandboxRunner
from app.sandbox.service import SandboxService, SandboxSessionService, _failure_reason
from app.services.audit import AuditSink, PermissionDeniedContext, PermissionDeniedRecorder
from app.services.tools.types import SandboxRun, ToolCancellationPending
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
    bounded runner call, so cancellation can observe it. All tenant/owner scoping +
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
        accounting: SandboxAccounting | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        lease_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        self._accounting = accounting
        self._retry_sleep, self._lease_sleep = retry_sleep, lease_sleep
        # R3-002 on the chat Stop path, matching the sandbox service: every
        # unconfirmed kill attempt is logged, but only the first failure per
        # execution writes an error audit, so an unreachable runner cannot
        # flood the append-only trail through this callback's retries.
        self._kill_attempts: dict[UUID, int] = {}
        self._kill_failure_audited: set[UUID] = set()

    async def submit(self, *, code: str, packages: tuple[str, ...] = ()) -> SandboxRun:
        """Run ``code`` to a terminal status; stream its output; return the summary.

        Creates the ``queued`` ``code_run`` (linked to the parent session + the
        assistant message via ``trace_id``), submits it to the sandbox service, then
        re-reads the terminal record and emits its captured stdout/stderr as
        ``code_output`` chunks followed by exactly one ``code_result``. Package
        requirements are policy-checked and installed by the runner before
        tenant inputs are staged. The runner enforces the effective execution deadline.
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
                    accounting=self._accounting,
                    retry_sleep=self._retry_sleep,
                    lease_sleep=self._lease_sleep,
                    cancellation_cleanup=self._cancel_run,
                )
                # The service walks the row through the deny-by-default gate → running
                # → terminal, capturing stdout/stderr/exit/artifacts and auditing every
                # transition. It commits running state before the bounded runner call
                # and never raises for a run concern (a crash becomes ``failed``).
                #
                # ``execute_outcome`` (not ``execute``) because the typed
                # ``reason_code`` is NOT a column on ``code_runs`` — only the refusal's
                # prose is, as ``stderr``. Reading it from the outcome is what lets the
                # tool put the stable code in the ``tool_invocations`` row instead of a
                # truncated sentence (issue #502).
                try:
                    outcome = await service.execute_outcome(session, run_id)
                except asyncio.CancelledError:
                    # Closing the HTTP await alone cannot terminate code in the
                    # runner. The service already confirmed the kill when the
                    # cancellation landed inside the runner call (the callback
                    # above); this covers a cancellation landing anywhere else
                    # in submission. Commit the killed state before the answer
                    # acknowledges Stop.
                    try:
                        await session.rollback()
                        await self._cancel_run(run_id)
                    except (Exception, asyncio.CancelledError) as exc:
                        # A failed kill must bypass the ordinary tool-error wrapper.
                        # A second cancellation can interrupt the kill await too;
                        # it still does not confirm that remote execution stopped.
                        # Retain the exact execution for producer-owned cleanup;
                        # only confirmed cleanup may acknowledge successful Stop.
                        raise ToolCancellationPending(partial(self._cancel_run, run_id)) from exc
                    raise
                terminal = await code_runs.get(run_id)
                await session.commit()
            except ToolCancellationPending:
                # Pending cleanup carries the execution's admission
                # ownership onward; a rollback await here must never be
                # allowed to replace it with a bare cancellation (TF3).
                raise
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

    async def _cancel_run(self, run_id: UUID, ownership: AdmissionOwnership | None = None) -> None:
        """Confirm a kill and commit its state, with no transaction held on retry.

        ``ownership`` is the admitted execution's one admission object
        (TF3): when the sandbox service hands cleanup onward it passes the
        ownership, so this callback shares the service loop's attempt
        counter, first-failure error-audit latch and first-cause
        classification. Without it (a cancellation landing outside the
        admitted execution, where no service loop ever ran) the runner's
        own per-run latch below is the only ledger and the cause is the
        producer cancellation itself.
        """
        accounting_loss = (
            ownership is not None and ownership.cause == SANDBOX_REASON_ACCOUNTING_UNAVAILABLE
        )
        async with self._sessionmaker() as session:
            await bind_tenant(session, self._tenant_id)
            lifecycle = SandboxSessionService(
                session,
                tenant_id=self._tenant_id,
                owner_id=self._owner_id,
                runner=self._runner,
                settings=self._settings,
                denials=PermissionDeniedContext(
                    PermissionDeniedRecorder(
                        db_session.get_durable_audit_transactions(self._settings),
                        tenant_id=self._tenant_id,
                        request_session=session,
                    ),
                    actor=AuditActor.system(),
                    request_id=self._request_id,
                    source_ip=self._source_ip,
                ),
            )
            runs = CodeRunRepository(session, self._tenant_id)
            current = await runs.get(run_id)
            if current is None:
                raise RuntimeError("Sandbox cleanup execution disappeared.")
            if current.status in (CodeRunStatus.QUEUED, CodeRunStatus.RUNNING):
                try:
                    if accounting_loss:
                        await self._confirm_accounting_kill(session, lifecycle, runs, current)
                    else:
                        await lifecycle.cancel(run_id)
                    await session.commit()
                except (Exception, asyncio.CancelledError) as exc:
                    # A failed kill must not go unrecorded (TR1-001), but only
                    # the first failure per execution writes the error audit
                    # (R3-002, as in the service loop); every attempt is still
                    # logged. Only the stable reason code is persisted — no
                    # raw transport diagnostics. The latch is the ownership's
                    # when handed one (shared with the service loop, one
                    # error row per execution) and is marked only after the
                    # commit lands.
                    reason_code = (
                        "sandbox_cancel_interrupted"
                        if isinstance(exc, asyncio.CancelledError)
                        else _failure_reason(exc)
                    )
                    if ownership is not None:
                        attempt = ownership.next_attempt()
                    else:
                        attempt = self._kill_attempts.get(run_id, 0) + 1
                        self._kill_attempts[run_id] = attempt
                    log.warning(
                        "sandbox.kill_unconfirmed",
                        code_run_id=str(run_id),
                        reason_code=reason_code,
                        attempt=attempt,
                    )
                    await session.rollback()
                    await bind_tenant(session, self._tenant_id)
                    auditing = (
                        not ownership.failure_audited
                        if ownership is not None
                        else run_id not in self._kill_failure_audited
                    )
                    if auditing:
                        await AuditSink(AuditEventRepository(session, self._tenant_id)).emit(
                            action=AuditAction.CODE_RUN_CANCELLED,
                            actor=AuditActor.user(self._owner_id),
                            resource_type="code_run",
                            resource_id=str(run_id),
                            outcome=AuditOutcome.ERROR,
                            request_id=self._request_id,
                            source_ip=self._source_ip,
                            metadata={"reason_code": reason_code},
                        )
                    await session.commit()
                    if auditing:
                        if ownership is not None:
                            ownership.mark_failure_audited()
                        else:
                            self._kill_failure_audited.add(run_id)
                    raise

    async def _confirm_accounting_kill(
        self,
        session: AsyncSession,
        lifecycle: SandboxSessionService,
        runs: CodeRunRepository,
        current: CodeRun,
    ) -> None:
        """Confirm an accounting-loss kill on the deferred chat path (TF3).

        Mirrors the sandbox service loop's accounting terminal exactly —
        the same runner kill on the same execution identity, then
        ``KILLED`` / ``sandbox_accounting_unavailable`` with a
        ``code_run.finished`` error audit — because the lifecycle user
        cancel would misrecord a system kill as an allowed
        ``code_run.cancelled``. The first established cause (accounting
        loss, latched before any Stop) wins; the chat turn still reflects
        the user's Stop (spec 0028).
        """
        if current.sandbox_session_id is not None and current.sandbox_generation is not None:
            sandbox = await SandboxSessionRepository(session, self._tenant_id).get(
                current.sandbox_session_id
            )
            await self._runner.cancel(
                lifecycle._build_spec(  # noqa: SLF001 - same owning module
                    sandbox_session_id=current.sandbox_session_id,
                    generation=current.sandbox_generation,
                    image=current.image_digest
                    or (
                        sandbox.image_digest
                        if sandbox is not None
                        else self._settings.sandbox_image
                    ),
                ),
                current.id,
            )
        await runs.mark_terminal(
            current.id,
            status=CodeRunStatus.KILLED,
            finished_at=datetime.now(UTC),
            stderr=sandbox_reason_public_message(SANDBOX_REASON_ACCOUNTING_UNAVAILABLE),
            termination_reason=SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
        )
        await AuditSink(AuditEventRepository(session, self._tenant_id)).emit(
            action=AuditAction.CODE_RUN_FINISHED,
            actor=AuditActor.user(self._owner_id),
            resource_type="code_run",
            resource_id=str(current.id),
            outcome=AuditOutcome.ERROR,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "status": CodeRunStatus.KILLED.value,
                "termination_reason": SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
                "reason_code": SANDBOX_REASON_ACCOUNTING_UNAVAILABLE,
                "effective_limits": current.effective_limits,
            },
        )

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
