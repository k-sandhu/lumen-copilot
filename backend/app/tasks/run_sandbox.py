"""Sandbox code-run Celery task — execute agent-authored Python off the request path (#230).

The thin Celery wrapper around the sandbox execution core
(:meth:`app.sandbox.service.SandboxService.execute`), mirroring ``ingest`` /
``run_assistant`` (ADR-0013 §4): the ``run_python`` tool (#231) / an internal caller
creates a ``queued`` :class:`~app.domain.entities.CodeRun` and enqueues
``lumen.run_sandbox(code_run_id, tenant_id)``; the agentic loop never runs code
inline. **The only place the sandbox task is defined or enqueued** (ADR-0004: tasks
live in ``tasks/``).

The worker calls the dedicated ``sandbox-runner`` service over its internal HTTP API
(ADR-0013 §1) — it holds **no** Docker socket. Runs on a fresh event loop via
``run_task`` (engine disposed after each run so no pooled connection outlives its
loop, #140).

Crash safety (ADR-0013 §5, INV-8): the execution core always writes a terminal
``code_runs`` status — a defect that escapes it is caught here and the run is failed,
never left ``running``. Ids are passed as strings (Celery serializes JSON, not UUIDs)
and parsed back to ``UUID``.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.core.config import Settings, get_settings
from app.db.repositories import CodeRunRepository
from app.db.session import tenant_session_scope
from app.domain.entities import CodeRunStatus
from app.sandbox.runner import HttpSandboxRunner
from app.sandbox.service import SandboxService
from app.storage import ObjectStore
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task


async def execute_code_run_async(
    code_run_id: UUID,
    tenant_id: UUID,
    *,
    settings: Settings,
) -> CodeRunStatus:
    """Execute one sandbox code run end-to-end, tenant-scoped (the async core).

    Runs under ``tenant_session_scope(tenant_id)`` so the whole run executes **as its
    tenant** with the RLS backstop armed (INV-1). Composes the live
    :class:`HttpSandboxRunner` (the worker's only container-engine surface — an
    internal HTTP hop to the dedicated runner, no Docker socket) and the object store,
    then drives :meth:`SandboxService.execute`. The ``owner_id`` is the run's
    principal, read from the durable row inside the scope (never request input) — the
    sandbox runs code on that user's behalf. Returns the terminal status.
    """
    runner = HttpSandboxRunner(settings.sandbox_runner_url)
    object_store = ObjectStore(settings)
    async with tenant_session_scope(tenant_id) as session:
        run = await CodeRunRepository(session, tenant_id).get(code_run_id)
        if run is None:
            # The run row was deleted / never existed in this tenant — idempotent no-op.
            return CodeRunStatus.FAILED
        service = SandboxService(
            tenant_id=tenant_id,
            owner_id=run.owner_id,
            runner=runner,
            object_store=object_store,
            settings=settings,
        )
        return await service.execute(session, code_run_id)


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.run_sandbox",
    bind=True,
    acks_late=True,
    max_retries=0,
)
def run_sandbox(self: object, code_run_id: str, tenant_id: str) -> dict[str, object]:
    """Celery entrypoint: execute one sandbox code run (the sync wrapper).

    Drives :func:`execute_code_run_async` on a fresh event loop via
    :func:`app.tasks.runner.run_task`. The core is crash-safe (it writes a terminal
    status on any failure), so the task does not retry: a run's failure is a
    **queryable terminal** (``status=failed``/``timeout``/``killed``/``denied``), not a
    redelivered message (ADR-0013 §5 — the dead-letter is a row). Returns the terminal.
    """
    log = structlog.get_logger(__name__)
    cid = UUID(code_run_id)
    tid = UUID(tenant_id)
    try:
        status = run_task(execute_code_run_async(cid, tid, settings=get_settings()))
    except Exception as exc:  # noqa: BLE001 — the message is acknowledged; the row is terminal
        log.error("run_sandbox.failed", code_run_id=code_run_id, error_type=type(exc).__name__)
        return {"code_run_id": code_run_id, "status": CodeRunStatus.FAILED.value}
    return {"code_run_id": code_run_id, "status": status.value}


def enqueue_code_run(code_run_id: UUID, tenant_id: UUID) -> None:
    """Enqueue a sandbox code run for execution (the seam the ``run_python`` tool calls).

    The single enqueue point (ADR-0004: tasks are enqueued only from ``tasks/``).
    Called **after-commit** once the ``queued`` ``code_runs`` row is durable so the
    worker drives it off the request path. Ids are passed as strings (Celery's JSON
    serializer).

    **Best-effort, bounded against the broker** (mirrors ``enqueue_run``): it runs
    after the run row has committed, so a transient broker outage must neither turn a
    successful enqueue into a 500 nor block the caller. The publish is on a
    bounded-reconnect connection so an unreachable broker raises
    ``kombu.exceptions.OperationalError`` in seconds; that is logged and swallowed —
    the run is left ``queued``. A *programming* error still propagates. This keeps the
    tool/API tests offline-safe: with no broker the publish fails fast and the run
    stays ``queued``.
    """
    from kombu.exceptions import OperationalError

    log = structlog.get_logger(__name__)
    try:
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
            run_sandbox.apply_async(
                args=(str(code_run_id), str(tenant_id)),
                connection=connection,
                retry=False,
            )
    except OperationalError as exc:
        log.warning(
            "run_sandbox.enqueue_failed",
            code_run_id=str(code_run_id),
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )
