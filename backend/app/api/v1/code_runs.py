"""Code-runs routes — inspect or cancel a sandbox code run (ADR-0020, issue #457).

Contract-first: the shape matches the **frozen** ``contracts/openapi.yaml``
``/code-runs/{codeRunId}`` surface (``GET`` → ``CodeRun``; active runs may be
explicitly cancelled). This module exposes a
module-level ``router`` and is **auto-discovered** (``api/v1/__init__.py`` scans for
it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all orchestration
(ownership/tenancy enforcement and cancellation/reset) lives in
``app.sandbox.service``; this layer only (de)serialises.

**Runs are agent-mediated (ADR-0020).** There is deliberately **no** public
"execute arbitrary code" endpoint — the ``run_python`` tool (#231, highest risk
tier, admin-gated) enqueues runs off the request path. This read surface exists so
the inspector UI (#232) / debuggable-runs (E6-5) can show what the agent ran and
what it produced.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A cross-tenant /
non-owned run id is reported as **404** (existence non-disclosure): the service
raises ``NotFoundError``, mapped by the global handler. A malformed (non-uuid) id is
**422** (FastAPI path validation).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.deps import CurrentTenant, CurrentUser, DbSession, SettingsDep
from app.domain.entities import CodeRun, CodeRunStatus, ResourceUsage
from app.sandbox.runner import HttpSandboxRunner
from app.sandbox.service import SandboxReadService, SandboxSessionService

router = APIRouter(prefix="/code-runs", tags=["code-runs"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class ResourceUsageResponse(BaseModel):
    """``#/components/schemas/ResourceUsage`` — measured consumption (best-effort)."""

    model_config = {"extra": "forbid"}

    peak_memory_bytes: int | None = None
    cpu_time_ms: int | None = None
    max_pids: int | None = None
    output_bytes: int | None = None


class CodeRunResponse(BaseModel):
    """``#/components/schemas/CodeRun`` — one sandbox code run, for inspection."""

    model_config = {"extra": "forbid"}

    id: UUID
    session_id: UUID | None = None
    sandbox_session_id: UUID | None = None
    sandbox_generation: int | None = None
    status: CodeRunStatus
    code: str
    requested_packages: list[str]
    stdout: str
    stderr: str
    artifact_ids: list[UUID]
    created_at: datetime
    exit_code: int | None = None
    duration_ms: int | None = None
    resource_usage: ResourceUsageResponse | None = None
    image_digest: str | None = None
    trace_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _usage_to_response(usage: ResourceUsage | None) -> ResourceUsageResponse | None:
    if usage is None:
        return None
    return ResourceUsageResponse(
        peak_memory_bytes=usage.peak_memory_bytes,
        cpu_time_ms=usage.cpu_time_ms,
        max_pids=usage.max_pids,
        output_bytes=usage.output_bytes,
    )


def _to_response(run: CodeRun) -> CodeRunResponse:
    return CodeRunResponse(
        id=run.id,
        session_id=run.session_id,
        sandbox_session_id=run.sandbox_session_id,
        sandbox_generation=run.sandbox_generation,
        status=run.status,
        code=run.code,
        requested_packages=list(run.requested_packages),
        stdout=run.stdout,
        stderr=run.stderr,
        artifact_ids=run.artifact_ids,
        created_at=run.created_at,
        exit_code=run.exit_code,
        duration_ms=run.duration_ms,
        resource_usage=_usage_to_response(run.resource_usage),
        image_digest=run.image_digest,
        trace_id=run.trace_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


@router.get(
    "/{code_run_id}", response_model=CodeRunResponse, response_model_exclude_none=True
)
async def get_code_run(
    code_run_id: UUID,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
) -> CodeRunResponse:
    """Inspect one code run — status, code, output, timing, artifacts; not visible → 404."""
    service = SandboxReadService(session, tenant_id=tenant_id, owner_id=principal.user_id)
    run = await service.get(code_run_id)
    return _to_response(run)


@router.post(
    "/{code_run_id}/cancel",
    response_model=CodeRunResponse,
    response_model_exclude_none=True,
)
async def cancel_code_run(
    code_run_id: UUID,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> CodeRunResponse:
    """Explicitly cancel an active run and advance its sandbox generation."""
    service = SandboxSessionService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        runner=HttpSandboxRunner(settings.sandbox_runner_url),
        settings=settings,
    )
    run = await service.cancel(code_run_id)
    await session.commit()
    return _to_response(run)
