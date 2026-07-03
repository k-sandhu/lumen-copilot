"""Headless agent-run Celery task — execute an assistant off the request path (#235).

The thin Celery wrapper around the headless-run execution core
(:func:`app.services.runs_service.execute_run`), mirroring ``ingest``/``sync_source``
(ADR-0015 §4): a scheduled fire (Beat, #236) or a run-now (API / test harness) both
create a ``queued`` :class:`~app.domain.entities.Run` and enqueue
``lumen.run_assistant(run_id, tenant_id)``; neither runs the agentic loop inline.

**The only place the run task is defined or enqueued** (ADR-0004: tasks live in
``tasks/``). The service creates the run row; the caller (router / scheduler) calls
:func:`enqueue_run` after-commit so the request returns immediately and a broker
outage never rolls back the queued run.

Crash safety (ADR-0015 §5, INV-8): the execution core always writes a terminal run
status — a defect that escapes it is caught here and the run is failed, never left
``running``. Ids are passed as strings (Celery serializes JSON, not UUIDs) and parsed
back to ``UUID``. Runs on a fresh event loop via ``run_task`` (engine disposed after
each run so no pooled connection outlives its loop, #140).
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.core.config import get_settings
from app.domain.entities import RunStatus
from app.services.runs_service import execute_run
from app.tasks.celery_app import celery_app
from app.tasks.runner import run_task


@celery_app.task(  # type: ignore[misc]  # celery's task decorator is untyped
    name="lumen.run_assistant",
    bind=True,
    acks_late=True,
    max_retries=0,
)
def run_assistant(self: object, run_id: str, tenant_id: str) -> dict[str, object]:
    """Celery entrypoint: execute one headless run (the sync wrapper).

    Drives :func:`execute_run` on a fresh event loop via
    :func:`app.tasks.runner.run_task`. The core is crash-safe (it writes a terminal
    status on any failure), so the task does not retry: a run's failure is a
    **queryable terminal** (``status=failed`` with a typed error), not a redelivered
    message (ADR-0015 §5 — the dead-letter is a row). Returns the terminal status.
    """
    log = structlog.get_logger(__name__)
    rid = UUID(run_id)
    tid = UUID(tenant_id)
    try:
        status = run_task(execute_run(rid, tid, settings=get_settings()))
    except Exception as exc:  # noqa: BLE001 — the message is acknowledged; the run row is terminal
        # ``execute_run`` writes a terminal itself; if the wrapper still sees an
        # exception (e.g. loop setup), log the type only and report failed rather
        # than redelivering forever.
        log.error("run_assistant.failed", run_id=run_id, error_type=type(exc).__name__)
        return {"run_id": run_id, "status": RunStatus.FAILED.value}
    return {"run_id": run_id, "status": status.value}


def enqueue_run(run_id: UUID, tenant_id: UUID) -> None:
    """Enqueue a headless run for execution (the seam the router / scheduler calls).

    The single enqueue point (ADR-0004: tasks are enqueued only from ``tasks/``).
    Called **after-commit** once the ``queued`` run row is durable so the worker
    drives it off the request path. Ids are passed as strings (Celery's JSON
    serializer).

    **Best-effort, bounded against the broker** (mirrors ``enqueue_ingestion``): it
    runs after the run row has committed, so a transient broker outage must neither
    turn a successful ``run-now`` into a 500 nor block the response. The publish is
    on a bounded-reconnect connection so an unreachable broker raises
    ``kombu.exceptions.OperationalError`` in seconds; that is logged and swallowed —
    the run is left ``queued`` (a re-drive/sweep is out of scope, ADR-0015 §5 fences).
    A *programming* error still propagates. This keeps the run-now API tests
    offline-safe: with no broker the publish fails fast and the run stays ``queued``.
    """
    from kombu.exceptions import OperationalError

    log = structlog.get_logger(__name__)
    try:
        with celery_app.connection_for_write() as connection:
            connection.ensure_connection(max_retries=1, timeout=2)
            run_assistant.apply_async(
                args=(str(run_id), str(tenant_id)),
                connection=connection,
                retry=False,
            )
    except OperationalError as exc:
        log.warning(
            "run_assistant.enqueue_failed",
            run_id=str(run_id),
            tenant_id=str(tenant_id),
            error=type(exc).__name__,
        )
