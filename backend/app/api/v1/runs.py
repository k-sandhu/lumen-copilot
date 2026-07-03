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

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.api.deps import CurrentTenant, CurrentUser, DbSession
from app.db.repositories import CitationView
from app.domain.entities import Run, RunStatus, RunStep, RunTrigger
from app.services.runs_service import RunDetail, RunPage, RunsReadService

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
    *, session: DbSession, principal: CurrentUser, tenant_id: CurrentTenant
) -> RunsReadService:
    return RunsReadService(session, tenant_id=tenant_id, owner_id=principal.user_id)


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
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> RunResponse:
    """Run detail — status, inputs, transcript, citations, error; not visible → 404."""
    service = _build_service(session=session, principal=principal, tenant_id=tenant_id)
    detail = await service.get(run_id)
    return _detail_to_response(detail)
