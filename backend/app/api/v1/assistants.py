"""Assistants routes — CRUD + publish / versions / rollback (ADR-0011, issue #211).

Contract-first: shapes match the frozen ``contracts/openapi.yaml`` ``/assistants*``
surface (``AssistantCreate`` / ``AssistantUpdate`` → ``Assistant``;
``AssistantList``; ``AssistantVersion`` / ``AssistantVersionList``;
``AssistantPublishRequest`` / ``AssistantRollbackRequest``). This module exposes a
module-level ``router`` and is **auto-discovered** (``api/v1/__init__.py`` scans
for it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all
orchestration (ownership/tenancy enforcement, allow-list + knowledge-scope
validation, publish rule, versioning/rollback, audit, the cursor codec) lives in
``services.assistants_service``; this layer only (de)serialises and threads the
correlation context.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A cross-tenant /
non-owned / non-granted id is reported as **404** (existence non-disclosure): the
service raises ``NotFoundError``, mapped by the global handler. A malformed body,
an unknown tool in the allow-list, a scope id the caller cannot access, a publish
without a distinct backup owner, or an unknown rollback version is **422** (INV-8)
— matching the frozen contract, which specifies 422 for the publish illegal
transition.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    LLMGatewayDep,
    extract_request_id,
    get_llm_gateway,
)
from app.domain.entities import (
    Assistant,
    AssistantStatus,
    AssistantVersion,
    AutonomyLevel,
    KnowledgeMode,
    KnowledgeScope,
)
from app.services.assistant_draft_service import AssistantDraftService, DraftResult
from app.services.assistant_test_service import AssistantTestService, AssistantTestTrace
from app.services.assistants_service import (
    UNSET,
    AssistantPage,
    AssistantsService,
    VersionPage,
)

router = APIRouter(prefix="/assistants", tags=["assistants"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------




class KnowledgeScopeModel(BaseModel):
    """``#/components/schemas/KnowledgeScope`` — the narrowing retrieval filter."""

    model_config = {"extra": "forbid"}

    collectionIds: list[UUID] = Field(default_factory=list)  # noqa: N815 — contract camelCase
    sourceIds: list[UUID] = Field(default_factory=list)  # noqa: N815 — contract camelCase
    modes: list[KnowledgeMode] = Field(default_factory=list)

    def to_domain(self) -> KnowledgeScope:
        return KnowledgeScope(
            collection_ids=tuple(self.collectionIds),
            source_ids=tuple(self.sourceIds),
            modes=tuple(self.modes),
        )

    @classmethod
    def from_domain(cls, scope: KnowledgeScope) -> KnowledgeScopeModel:
        return cls(
            collectionIds=list(scope.collection_ids),
            sourceIds=list(scope.source_ids),
            modes=list(scope.modes),
        )


class AssistantResponse(BaseModel):
    """``#/components/schemas/Assistant`` — the mutable head projection."""

    model_config = {"extra": "forbid"}

    id: UUID
    name: str
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    knowledgeScope: KnowledgeScopeModel  # noqa: N815 — contract camelCase
    toolAllowlist: list[str]  # noqa: N815 — contract camelCase
    autonomyLevel: AutonomyLevel  # noqa: N815 — contract camelCase
    owner: UUID
    backupOwner: UUID | None = None  # noqa: N815 — contract camelCase
    status: AssistantStatus
    version: int | None = None
    created_at: datetime
    updated_at: datetime


class AssistantListResponse(BaseModel):
    """``#/components/schemas/AssistantList``."""

    model_config = {"extra": "forbid"}

    items: list[AssistantResponse]
    next_cursor: str | None = None


class AssistantVersionResponse(BaseModel):
    """``#/components/schemas/AssistantVersion`` — an immutable snapshot."""

    model_config = {"extra": "forbid"}

    id: UUID
    assistant_id: UUID
    version: int
    config: dict[str, object]
    author: UUID | None = None
    notes: str | None = None
    diff_summary: str | None = None
    created_at: datetime


class AssistantVersionListResponse(BaseModel):
    """``#/components/schemas/AssistantVersionList``."""

    model_config = {"extra": "forbid"}

    items: list[AssistantVersionResponse]
    next_cursor: str | None = None


class AssistantCreateRequest(BaseModel):
    """``#/components/schemas/AssistantCreate`` — only ``name`` required."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    knowledgeScope: KnowledgeScopeModel | None = None  # noqa: N815 — contract camelCase
    toolAllowlist: list[str] | None = None  # noqa: N815 — contract camelCase
    autonomyLevel: AutonomyLevel | None = None  # noqa: N815 — contract camelCase
    backupOwner: UUID | None = None  # noqa: N815 — contract camelCase


class AssistantUpdateRequest(BaseModel):
    """``#/components/schemas/AssistantUpdate`` — partial; at least one field.

    Uses ``model_fields_set`` to distinguish an omitted field from an explicit
    ``null`` (a nullable field may be cleared), threading ``UNSET`` to the service
    for untouched fields so a PATCH never overwrites an unmentioned attribute.
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    knowledgeScope: KnowledgeScopeModel | None = None  # noqa: N815 — contract camelCase
    toolAllowlist: list[str] | None = None  # noqa: N815 — contract camelCase
    autonomyLevel: AutonomyLevel | None = None  # noqa: N815 — contract camelCase
    backupOwner: UUID | None = None  # noqa: N815 — contract camelCase


class AssistantPublishRequest(BaseModel):
    """``#/components/schemas/AssistantPublishRequest`` — optional release note."""

    model_config = {"extra": "forbid"}

    notes: str | None = None


class AssistantRollbackRequest(BaseModel):
    """``#/components/schemas/AssistantRollbackRequest`` — the prior version to restore."""

    model_config = {"extra": "forbid"}

    version: int = Field(ge=1)
    notes: str | None = None


class AssistantDraftRequest(BaseModel):
    """``#/components/schemas/AssistantDraftRequest`` — the plain-language ask."""

    model_config = {"extra": "forbid"}

    description: str = Field(min_length=1, max_length=4000)


class AssistantDraftConfigModel(BaseModel):
    """``#/components/schemas/AssistantDraftConfig`` — the drafted, editable config.

    The subset of ``AssistantVersionConfig`` the editor pre-fills (no owner/status/
    version — nothing is created until the user saves). ``model`` ``null`` ⇒ the
    smart server default; the scope + allow-list default empty (the safe draft).
    """

    model_config = {"extra": "forbid"}

    name: str
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    knowledgeScope: KnowledgeScopeModel  # noqa: N815 — contract camelCase
    toolAllowlist: list[str]  # noqa: N815 — contract camelCase
    autonomyLevel: AutonomyLevel  # noqa: N815 — contract camelCase


class AssistantDraftResponse(BaseModel):
    """``#/components/schemas/AssistantDraft`` — the draft + clarifications/notes.

    ``draft`` is the config the FE loads into ``AssistantEditor`` for review;
    ``clarifications`` are the questions the builder asks for missing scope / owner
    / risk (E6-3); ``notes`` explain any field omitted because it referenced an
    unknown tool or an unseeable collection/source; ``warnings`` flag any drafted
    high-risk tool the user should acknowledge before publish.
    """

    model_config = {"extra": "forbid"}

    draft: AssistantDraftConfigModel
    clarifications: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --- Serialisation helpers --------------------------------------------------


def _to_response(assistant: Assistant) -> AssistantResponse:
    return AssistantResponse(
        id=assistant.id,
        name=assistant.name,
        description=assistant.description,
        instructions=assistant.instructions,
        model=assistant.model,
        knowledgeScope=KnowledgeScopeModel.from_domain(assistant.knowledge_scope),
        toolAllowlist=list(assistant.tool_allowlist),
        autonomyLevel=assistant.autonomy_level,
        owner=assistant.owner_id,
        backupOwner=assistant.backup_owner_id,
        status=assistant.status,
        version=assistant.current_version,
        created_at=assistant.created_at,
        updated_at=assistant.updated_at,
    )


def _to_list_response(page: AssistantPage) -> AssistantListResponse:
    return AssistantListResponse(
        items=[_to_response(a) for a in page.items],
        next_cursor=page.next_cursor,
    )


def _to_version_response(version: AssistantVersion) -> AssistantVersionResponse:
    return AssistantVersionResponse(
        id=version.id,
        assistant_id=version.assistant_id,
        version=version.version,
        config=version.config,
        author=version.author_id,
        notes=version.notes,
        diff_summary=version.diff_summary,
        created_at=version.created_at,
    )


def _to_version_list_response(page: VersionPage) -> AssistantVersionListResponse:
    return AssistantVersionListResponse(
        items=[_to_version_response(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


def _to_draft_response(result: DraftResult) -> AssistantDraftResponse:
    draft = result.draft
    return AssistantDraftResponse(
        draft=AssistantDraftConfigModel(
            name=draft.name,
            description=draft.description,
            instructions=draft.instructions,
            model=draft.model,
            knowledgeScope=KnowledgeScopeModel(
                collectionIds=list(draft.collection_ids),
                sourceIds=list(draft.source_ids),
                modes=[],
            ),
            toolAllowlist=list(draft.tool_allowlist),
            autonomyLevel=draft.autonomy_level,
        ),
        clarifications=list(result.clarifications),
        notes=list(result.notes),
        warnings=list(result.warnings),
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    request: Request,
) -> AssistantsService:
    """Assemble the per-request service from the identity + audit seams."""
    return AssistantsService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        roles=principal.roles,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=AssistantListResponse, response_model_exclude_none=True)
async def list_assistants(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    status_filter: Annotated[AssistantStatus | None, Query(alias="status")] = None,
) -> AssistantListResponse:
    """List the caller's own assistants (cursor-paginated, newest first)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    page = await service.list_(cursor=cursor, limit=limit, status=status_filter)
    return _to_list_response(page)


@router.post(
    "",
    response_model=AssistantResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_assistant(
    body: AssistantCreateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> AssistantResponse:
    """Create a draft assistant owned by the caller (unknown tool / bad scope → 422)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    assistant = await service.create(
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        model=body.model,
        knowledge_scope=body.knowledgeScope.to_domain() if body.knowledgeScope else None,
        tool_allowlist=tuple(body.toolAllowlist or ()),
        autonomy_level=body.autonomyLevel or AutonomyLevel.SUGGEST,
        backup_owner_id=body.backupOwner,
    )
    await session.commit()
    return _to_response(assistant)


@router.post(
    "/draft",
    response_model=AssistantDraftResponse,
    response_model_exclude_none=True,
)
async def draft_assistant(
    body: AssistantDraftRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    llm: LLMGatewayDep,
) -> AssistantDraftResponse:
    """Draft an assistant config from a plain-language description (E6-1, #213).

    Creates **nothing** — returns a draft config the FE loads into the editor for
    review + save, plus clarifying questions (missing scope/owner/risk) and notes
    for any tool/scope the description named that was omitted (unknown tool → not
    in the draft; unseeable collection/source → not in the draft — deny-by-default).
    Owner-scoped; audited ``assistant.drafted``. A blank/oversize body → 422.
    """
    service = AssistantDraftService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        llm=llm,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    result = await service.draft(description=body.description)
    await session.commit()
    return _to_draft_response(result)


@router.get(
    "/{assistant_id}",
    response_model=AssistantResponse,
    response_model_exclude_none=True,
)
async def get_assistant(
    assistant_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> AssistantResponse:
    """Get one of the caller's assistants; not visible → 404 (INV-1/INV-2)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    assistant = await service.get(assistant_id)
    return _to_response(assistant)


@router.patch(
    "/{assistant_id}",
    response_model=AssistantResponse,
    response_model_exclude_none=True,
)
async def update_assistant(
    assistant_id: UUID,
    body: AssistantUpdateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> AssistantResponse:
    """Patch a draft assistant's working definition; not visible → 404, invalid → 422."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    fields_set = body.model_fields_set
    assistant = await service.update(
        assistant_id,
        name=body.name if "name" in fields_set else UNSET,
        description=body.description if "description" in fields_set else UNSET,
        instructions=body.instructions if "instructions" in fields_set else UNSET,
        model=body.model if "model" in fields_set else UNSET,
        knowledge_scope=(
            body.knowledgeScope.to_domain()
            if "knowledgeScope" in fields_set and body.knowledgeScope is not None
            else (KnowledgeScope.empty() if "knowledgeScope" in fields_set else UNSET)
        ),
        tool_allowlist=body.toolAllowlist if "toolAllowlist" in fields_set else UNSET,
        autonomy_level=body.autonomyLevel if "autonomyLevel" in fields_set else UNSET,
        backup_owner_id=body.backupOwner if "backupOwner" in fields_set else UNSET,
    )
    await session.commit()
    return _to_response(assistant)


@router.delete("/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assistant(
    assistant_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Delete an assistant head (version history preserved); not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    await service.delete(assistant_id)
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/{assistant_id}/publish",
    response_model=AssistantVersionResponse,
    response_model_exclude_none=True,
)
async def publish_assistant(
    assistant_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    body: AssistantPublishRequest | None = None,
) -> AssistantVersionResponse:
    """Publish a draft → published; without a distinct backup owner → 422 (INV-8)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    version = await service.publish(
        assistant_id, notes=body.notes if body is not None else None
    )
    await session.commit()
    return _to_version_response(version)


@router.get(
    "/{assistant_id}/versions",
    response_model=AssistantVersionListResponse,
    response_model_exclude_none=True,
)
async def list_assistant_versions(
    assistant_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AssistantVersionListResponse:
    """List an assistant's immutable versions (newest first); not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    page = await service.list_versions(assistant_id, cursor=cursor, limit=limit)
    return _to_version_list_response(page)


@router.post(
    "/{assistant_id}/rollback",
    response_model=AssistantVersionResponse,
    response_model_exclude_none=True,
)
async def rollback_assistant(
    assistant_id: UUID,
    body: AssistantRollbackRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
) -> AssistantVersionResponse:
    """Roll the head back to a prior version (creates a NEW head version); unknown version → 422."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    version = await service.rollback(assistant_id, version=body.version, notes=body.notes)
    await session.commit()
    return _to_version_response(version)


class AssistantTestRequest(BaseModel):
    """``#/components/schemas/AssistantTestRequest`` — a sample input for a test run."""

    model_config = {"extra": "forbid"}

    input: str | None = Field(default=None, max_length=8000)


class AssistantTestToolCall(BaseModel):
    """``#/components/schemas/AssistantTestToolCall`` — one tool call in the trace."""

    model_config = {"extra": "forbid"}

    callId: str  # noqa: N815 — contract camelCase
    tool: str | None = None
    args: dict[str, object] | None = None
    result: dict[str, object] | None = None


class AssistantTestTraceResponse(BaseModel):
    """``#/components/schemas/AssistantTestTrace`` — the debug trace of a read-only test run.

    The full trace the builder debug view renders (E6-5): the effective system
    ``prompt``, the sample ``input``, the resolved ``model``, the ``retrieval``
    grounding, each ``toolCalls`` entry (args + result — a T1 write is simulated,
    ``run_python`` is denied), the streamed ``outputs``, any typed ``errors``, whether
    the run ``succeeded``, and the wall-clock ``durationMs``. NO real side effect was
    produced.
    """

    model_config = {"extra": "forbid"}

    prompt: str
    input: str
    model: str
    retrieval: list[dict[str, object]]
    toolCalls: list[AssistantTestToolCall]  # noqa: N815 — contract camelCase
    outputs: str
    errors: list[dict[str, object]]
    succeeded: bool
    durationMs: int  # noqa: N815 — contract camelCase


def _to_test_trace_response(trace: AssistantTestTrace) -> AssistantTestTraceResponse:
    return AssistantTestTraceResponse(
        prompt=trace.prompt,
        input=trace.input,
        model=trace.model,
        retrieval=trace.retrieval,
        toolCalls=[
            AssistantTestToolCall(
                callId=str(c.get("callId", "")),
                tool=_opt_str(c.get("tool")),
                args=c.get("args") if isinstance(c.get("args"), dict) else None,
                result=c.get("result") if isinstance(c.get("result"), dict) else None,
            )
            for c in trace.tool_calls
        ],
        outputs=trace.outputs,
        errors=trace.errors,
        succeeded=trace.succeeded,
        durationMs=trace.duration_ms,
    )


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


@router.post(
    "/{assistant_id}/test",
    response_model=AssistantTestTraceResponse,
    response_model_exclude_none=True,
)
async def test_assistant(
    assistant_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    body: AssistantTestRequest | None = None,
) -> AssistantTestTraceResponse:
    """Preview/test/debug a draft assistant with NO real side effect (E6-5, issue #215).

    Runs the assistant's working (draft) config against a sample input with write-tier
    tools forced into simulate/deny mode (a T1 ``write_file`` is simulated — no
    artifact; ``run_python`` is denied — no code run), and returns the debug trace
    (prompt, retrieval, tool calls with args/results, outputs, errors, timing).
    Owner-scoped (owner-or-admin): a non-owned / cross-tenant id → 404 (existence
    non-disclosure, INV-1/INV-2). Audited ``assistant.tested`` (INV-6); nothing else
    is persisted.
    """
    service = AssistantTestService(
        session,
        principal=principal,
        gateway=get_llm_gateway(),
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )
    trace = await service.run_test(
        assistant_id, input_text=body.input if body is not None else None
    )
    # Commit the request session to persist ONLY the assistant.tested audit event (the
    # runtime's own transcript writes were rolled back inside the service).
    await session.commit()
    return _to_test_trace_response(trace)
