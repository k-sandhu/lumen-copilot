"""Runs routes — the run inbox (list) + run detail (ADR-0015 §8, issue #235).

Contract-first: shapes match the **frozen** ``contracts/openapi.yaml`` ``/runs*``
surface (``GET /runs`` → ``RunList``; ``GET /runs/{runId}`` → ``Run``). This module
exposes a module-level ``router`` and is **auto-discovered** (``api/v1/__init__.py``
scans for it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all
orchestration (ownership/tenancy enforcement, the cursor codec, transcript +
citation hydration) lives in ``services.runs_service``; this layer only
(de)serialises.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A cross-tenant /
non-owned run id is reported as **404** (existence non-disclosure): the service
raises ``NotFoundError``, mapped by the global handler. Read-only — runs are
created by the scheduler (#236) / run-now, executed by the Celery task (#235), never
by these routes. **Live attach:** to watch an in-progress run, connect the existing
chat WebSocket with the run's ``id`` as the ``streamId`` (ADR-0015 §3) — no new
wire shape here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    extract_request_id,
)
from app.db.repositories import CitationView
from app.domain.entities import Run, RunStatus, RunStep, RunTrigger
from app.services.runs_service import (
    RunControlResult,
    RunDetail,
    RunPage,
    RunsControlService,
    RunsReadService,
)
from app.tasks.run_assistant import enqueue_run

router = APIRouter(prefix="/runs", tags=["runs"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class RunErrorResponse(BaseModel):
    """``#/components/schemas/RunError`` — a typed failure / escalation reason."""

    model_config = {"extra": "forbid"}

    code: str
    message: str


class RunStepResponse(BaseModel):
    """``#/components/schemas/RunStep`` — one ordered transcript step."""

    model_config = {"extra": "forbid"}

    seq: int
    kind: str
    payload: dict[str, object] | None = None
    created_at: datetime


class CitationResponse(BaseModel):
    """``#/components/schemas/Citation`` — a grounded passage reference (INV-3)."""

    model_config = {"extra": "forbid"}

    id: UUID
    document_id: UUID
    document_name: str
    chunk_id: UUID
    snippet: str
    char_start: int
    char_end: int
    score: float | None = None
    #: True when the caller may no longer retrieve the cited document (#558), in
    #: which case `snippet` and `document_name` are empty. The row is kept so a
    #: settled claim's provenance stays visible instead of silently disappearing.
    #: Mirrors the chat transcript's field — both project the SAME contract
    #: schema (`#/components/schemas/Citation`), and a parity test pins them.
    redacted: bool = False


class RunResponse(BaseModel):
    """``#/components/schemas/Run`` — a run record (list item or full detail)."""

    model_config = {"extra": "forbid"}

    id: UUID
    schedule_id: UUID | None = None
    assistant_id: UUID
    assistant_version_id: UUID | None = None
    trigger: RunTrigger
    status: RunStatus
    inputs: dict[str, object] | None = None
    summary: str | None = None
    message_id: UUID | None = None
    steps: list[RunStepResponse] | None = None
    citations: list[CitationResponse] | None = None
    outputs: dict[str, object] | None = None
    error: RunErrorResponse | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class RunListResponse(BaseModel):
    """``#/components/schemas/RunList``."""

    model_config = {"extra": "forbid"}

    items: list[RunResponse]
    next_cursor: str | None = None


class RunRerouteRequest(BaseModel):
    """``#/components/schemas/RunReroute`` — reassign an escalated run to another owner."""

    model_config = {"extra": "forbid"}

    to_owner_id: UUID


# --- Serialisation helpers --------------------------------------------------


def _step_to_response(step: RunStep) -> RunStepResponse:
    return RunStepResponse(
        seq=step.seq,
        kind=step.kind.value,
        payload=step.payload,
        created_at=step.created_at,
    )


def _citation_to_response(view: CitationView) -> CitationResponse:
    return CitationResponse(
        id=view.id,
        document_id=view.document_id,
        document_name=view.document_name,
        chunk_id=view.chunk_id,
        snippet=view.snippet,
        char_start=view.char_start,
        char_end=view.char_end,
        score=view.score,
        redacted=view.redacted,
    )


def _run_to_response(
    run: Run,
    *,
    steps: list[RunStep] | None = None,
    citations: list[CitationView] | None = None,
) -> RunResponse:
    """Project a run to the wire; ``steps``/``citations`` present only on the detail.

    The list view omits the transcript + citations (empty on ``/runs``, present on
    ``/runs/{id}`` — the frozen contract), so the inbox is cheap.
    """
    return RunResponse(
        id=run.id,
        schedule_id=run.schedule_id,
        assistant_id=run.assistant_id,
        assistant_version_id=run.assistant_version_id,
        trigger=run.trigger,
        status=run.status,
        inputs=run.inputs or None,
        summary=run.summary,
        message_id=run.message_id,
        steps=[_step_to_response(s) for s in steps] if steps is not None else None,
        citations=(
            [_citation_to_response(c) for c in citations] if citations is not None else None
        ),
        error=(
            RunErrorResponse(code=run.error.code, message=run.error.message)
            if run.error is not None
            else None
        ),
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
    )


def _list_to_response(page: RunPage) -> RunListResponse:
    return RunListResponse(
        items=[_run_to_response(r) for r in page.items],
        next_cursor=page.next_cursor,
    )


def _detail_to_response(detail: RunDetail) -> RunResponse:
    return _run_to_response(detail.run, steps=detail.steps, citations=detail.citations)


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory | None = None,
    request: Request | None = None,
) -> RunsReadService:
    """Assemble the run read service, with its audit seam when the caller audits.

    Only the DETAIL read can audit: it re-checks stored evidence against current
    permissions and records when it withholds some (#558, INV-6). The inbox list
    serves no citations, so threading a sink through it would be noise — the same
    split ``chat._build_service`` makes for the transcript.
    """
    audit_kwargs: dict[str, object] = {}
    if make_audit_sink is not None and request is not None:
        audit_kwargs = {
            "audit": make_audit_sink(tenant_id),
            # The envelope requires both (spec 0004 section 2.4).
            "request_id": extract_request_id(request) or "unknown",
            "source_ip": request.client.host if request.client else "unknown",
        }
    return RunsReadService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        **audit_kwargs,  # type: ignore[arg-type]
    )


def _build_control_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    request: Request,
) -> RunsControlService:
    """Assemble the escalation-handoff service from the identity + audit seams (#239)."""
    return RunsControlService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


def _control_to_response(result: RunControlResult) -> RunResponse:
    """Project the post-action run to the wire (no transcript — the list-item shape)."""
    return _run_to_response(result.run)


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=RunListResponse, response_model_exclude_none=True)
async def list_runs(
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    assistant_id: Annotated[UUID | None, Query()] = None,
    schedule_id: Annotated[UUID | None, Query()] = None,
    status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RunListResponse:
    """The run inbox — list the caller's runs (newest first), filterable (ADR-0015 §8)."""
    service = _build_service(session=session, principal=principal, tenant_id=tenant_id)
    page = await service.list_(
        cursor=cursor,
        limit=limit,
        assistant_id=assistant_id,
        schedule_id=schedule_id,
        status=status_filter,
    )
    return _list_to_response(page)


@router.get("/{run_id}", response_model=RunResponse, response_model_exclude_none=True)
async def get_run(
    run_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> RunResponse:
    """Run detail — status, inputs, transcript, citations, error; not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    detail = await service.get(run_id)
    # This read AUDITS when it withholds evidence the caller may no longer see
    # (#558). `AuditSink.emit` flushes but does not commit — the caller owns the
    # transaction — so an audited read must close its own or the row rolls back
    # with the request-scoped session (the #554 review's finding, on the
    # sibling surface).
    await session.commit()
    return _detail_to_response(detail)


# --- Escalation handoff: resume / cancel / reroute (E7-5, #239) -------------
# Owner-scoped human actions on an escalated run. Each: validate in → one service
# call → shape out; the router commits and (for resume/reroute) enqueues the run task
# after-commit (the run-now pattern) so a broker outage never rolls back the requeue.


@router.post(
    "/{run_id}/resume",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def resume_run(
    run_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> RunResponse:
    """Resume an escalated run (re-enqueue). Not escalated → 409; not visible → 404."""
    service = _build_control_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    result = await service.resume(run_id)
    await session.commit()
    if result.requeued:
        enqueue_run(result.run.id, tenant_id)
    return _control_to_response(result)


@router.post(
    "/{run_id}/cancel",
    response_model=RunResponse,
    response_model_exclude_none=True,
)
async def cancel_run(
    run_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> RunResponse:
    """Cancel an escalated run (close it). Not escalated → 409; not visible → 404."""
    service = _build_control_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    result = await service.cancel(run_id)
    await session.commit()
    return _control_to_response(result)


@router.post(
    "/{run_id}/reroute",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def reroute_run(
    run_id: UUID,
    body: RunRerouteRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> RunResponse:
    """Reroute an escalated run to another owner + re-enqueue. Bad target → 404/422."""
    service = _build_control_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    result = await service.reroute(run_id, to_owner_id=body.to_owner_id)
    await session.commit()
    if result.requeued:
        enqueue_run(result.run.id, tenant_id)
    return _control_to_response(result)
