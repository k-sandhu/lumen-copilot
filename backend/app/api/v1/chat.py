"""Chat routes — sessions CRUD + history + send (CC-6 #24 / CC-11 #26).

Contract-first: shapes match ``contracts/openapi.yaml`` (``ChatSessionCreate`` /
``ChatSessionUpdate`` → ``ChatSession``; ``MessageList``; ``SendMessageRequest`` →
202 ``SendMessageResponse``). Routers validate in → call **one** service → shape
out (ADR-0004): ownership/tenancy enforcement, the model allow-list, the cursor
codec, and persistence all live in ``services.chat_service``; the *answer* is
produced by ``services.chat_runtime`` and streamed over the WS backplane.

**Tenancy + ownership (spec 0004 §2.1/§2.2).** A session in another tenant or
owned by another user is reported as **404** (existence non-disclosure,
INV-1/INV-2): the service returns ``None`` and this router maps that to
``NotFoundError``, never 403. Every route requires the bearer token; an unknown
model on create/update/send is **422** (INV-8). Send returns **202** and kicks
off the agentic answer as a background task that streams over WS keyed by the
returned ``stream_id``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import (
    AuditSinkFactory,
    BackplaneDep,
    CurrentTenant,
    CurrentUser,
    DbSession,
    SettingsDep,
    authenticated_denial_context,
    extract_request_id,
    get_llm_gateway,
)
from app.core.config import Settings
from app.core.errors import DependencyError, NotFoundError
from app.core.logging import get_logger
from app.db.repositories import CitationView
from app.db.session import get_sessionmaker
from app.domain.chat import AskUserQuestion
from app.domain.entities import KnowledgeMode, Message, MessageRole, SandboxSession, ToolInvocation
from app.domain.llm import ChatMessage, Role
from app.realtime.backplane import Backplane
from app.sandbox.runner import HttpSandboxRunner
from app.sandbox.service import SandboxDisabledError, SandboxSessionService
from app.services.chat_composition import ChatCompositionOptions, build_chat_runtime
from app.services.chat_runtime import ChatRuntime
from app.services.chat_service import (
    ChatService,
    MessagePage,
    MessageView,
    SendResult,
    SessionPage,
    SessionView,
)
from app.tasks.summarize import enqueue_summarize

log = get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class ChatSessionCreate(BaseModel):
    """``#/components/schemas/ChatSessionCreate``."""

    model_config = {"extra": "forbid"}

    title: str | None = Field(default=None, max_length=200)
    model: str | None = None
    # Optional (additive; ADR-0011 §5): start the session from an assistant. The
    # server resolves it, pins its current published version, and injects its
    # instructions/tool-allowlist/knowledge-scope into the existing chat runtime.
    # Omitted ⇒ an ad-hoc session, unchanged.
    assistant_id: UUID | None = None


class ChatSessionUpdate(BaseModel):
    """``#/components/schemas/ChatSessionUpdate`` — partial; at least one field."""

    model_config = {"extra": "forbid"}

    title: str | None = Field(default=None, max_length=200)
    model: str | None = None

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> ChatSessionUpdate:
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided.")
        return self


class SandboxSessionResponse(BaseModel):
    """``#/components/schemas/SandboxSession``."""

    model_config = {"extra": "forbid"}

    status: Literal["not_created", "active", "closed", "error"]
    enabled: bool
    root_access: Literal[True] = True
    sandbox_session_id: UUID | None = None
    generation: int | None = None
    image_digest: str | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    closed_at: datetime | None = None


class EffectiveKnowledgeScopeAdjustableResponse(BaseModel):
    """``#/components/schemas/EffectiveKnowledgeScopeAdjustable`` (#581)."""

    model_config = {"extra": "forbid"}

    modes: Literal[False]
    collection_ids: Literal[False]
    source_ids: Literal[False]


class EffectiveKnowledgeScopeResponse(BaseModel):
    """``#/components/schemas/EffectiveKnowledgeScope`` (#581)."""

    model_config = {"extra": "forbid"}

    assistant_id: UUID
    assistant_version_id: UUID
    modes: list[KnowledgeMode]
    collection_ids: list[UUID]
    source_ids: list[UUID]
    adjustable: EffectiveKnowledgeScopeAdjustableResponse


class ChatSessionResponse(BaseModel):
    """``#/components/schemas/ChatSession``."""

    model_config = {"extra": "forbid"}

    id: UUID
    title: str
    model: str
    owner_id: UUID
    message_count: int
    created_at: datetime
    updated_at: datetime
    # The pinned version's effective scope (#581); null for ad-hoc chat.
    effective_scope: EffectiveKnowledgeScopeResponse | None = None


class ChatSessionListResponse(BaseModel):
    """``#/components/schemas/ChatSessionList``."""

    model_config = {"extra": "forbid"}

    items: list[ChatSessionResponse]
    next_cursor: str | None = None


class CitationResponse(BaseModel):
    """``#/components/schemas/Citation``."""

    model_config = {"extra": "forbid"}

    id: UUID
    document_id: UUID
    document_name: str
    chunk_id: UUID
    snippet: str
    char_start: int
    char_end: int
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    transcript_segment_id: UUID | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    score: float | None = None
    handle: str | None = None
    #: True when the caller may no longer retrieve the cited document (#536), in
    #: which case `snippet` and `document_name` are empty. The row is kept so a
    #: settled claim's provenance stays visible instead of silently disappearing.
    redacted: bool = False


class WebCitationResponse(BaseModel):
    """A model-visible web excerpt, without invented corpus identifiers."""

    model_config = {"extra": "forbid"}

    id: UUID
    handle: str
    url: str
    title: str
    snippet: str


class MessageToolInvocationResponse(BaseModel):
    """``#/components/schemas/MessageToolInvocation`` (#377).

    Tool name + outcome only — call arguments are never stored raw (the trace
    keeps a non-reversible hash, spec 0004 §2.4 discipline), so none are exposed.
    """

    model_config = {"extra": "forbid"}

    id: UUID
    tool_name: str
    ok: bool
    error: str | None = None
    result_summary: str | None = None
    duration_ms: int
    created_at: datetime


class AskUserOptionResponse(BaseModel):
    """``#/components/schemas/AskUserOption`` (spec 0006 #429)."""

    model_config = {"extra": "forbid"}

    label: str
    description: str | None = None


class AskUserQuestionResponse(BaseModel):
    """``#/components/schemas/AskUserQuestion`` (spec 0006 #429).

    The clarifying question an assistant turn ended with; the UI re-renders the
    clickable options from here after reload (active only while it is the
    conversation's last message).
    """

    model_config = {"extra": "forbid"}

    question: str
    options: list[AskUserOptionResponse]
    allow_free_text: bool


class MessageResponse(BaseModel):
    """``#/components/schemas/Message``."""

    model_config = {"extra": "forbid"}

    id: UUID
    session_id: UUID
    role: MessageRole
    content: str
    model: str | None = None
    citations: list[CitationResponse]
    web_citations: list[WebCitationResponse] = Field(default_factory=list)
    tool_invocations: list[MessageToolInvocationResponse]
    question: AskUserQuestionResponse | None = None
    created_at: datetime


class MessageListResponse(BaseModel):
    """``#/components/schemas/MessageList``."""

    model_config = {"extra": "forbid"}

    items: list[MessageResponse]
    next_cursor: str | None = None


class SessionUsageTotalsResponse(BaseModel):
    """``#/components/schemas/SessionUsageTotals`` (spec 0007 #429)."""

    model_config = {"extra": "forbid"}

    answers: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: int
    cache_write_tokens: int


class SessionUsageLastResponse(BaseModel):
    """``#/components/schemas/SessionUsageLast`` (spec 0007 #429)."""

    model_config = {"extra": "forbid"}

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: int
    cache_write_tokens: int
    # Final-turn window occupancy (#434 NEW-1): what the model's window
    # actually held, vs prompt_tokens' billing sum across loop turns. Absent
    # for legacy rows / providers that reported no usage.
    context_prompt_tokens: int | None = None


class SessionUsageResponse(BaseModel):
    """``#/components/schemas/SessionUsage`` (spec 0007 #429)."""

    model_config = {"extra": "forbid"}

    model: str
    totals: SessionUsageTotalsResponse
    last: SessionUsageLastResponse | None = None
    input_budget_tokens: int
    window_known: bool


class SendMessageRequest(BaseModel):
    """``#/components/schemas/SendMessageRequest``."""

    model_config = {"extra": "forbid"}

    content: str = Field(min_length=1)
    model: str | None = None
    collection_ids: list[UUID] | None = None
    # Pinned documents (spec 0007 #429): retrieval for THIS answer narrows to
    # these ids (an additional filter over the caller's allow-set — INV-2 still
    # applies inside retrieval/; an id the caller can't access contributes
    # nothing and discloses nothing). Additive; None/[] ⇒ unchanged behavior.
    document_ids: list[UUID] | None = Field(default=None, max_length=20)


class SendMessageResponse(BaseModel):
    """``#/components/schemas/SendMessageResponse`` — the persisted user msg + id."""

    model_config = {"extra": "forbid"}

    message: MessageResponse
    stream_id: str


# --- Serialisation helpers --------------------------------------------------


def _session_to_response(view: SessionView) -> ChatSessionResponse:
    s = view.session
    scope = view.effective_scope
    return ChatSessionResponse(
        id=s.id,
        title=s.title,
        model=s.model,
        owner_id=s.owner_id,
        message_count=view.message_count,
        created_at=s.created_at,
        updated_at=s.updated_at,
        effective_scope=(
            EffectiveKnowledgeScopeResponse(
                assistant_id=scope.assistant_id,
                assistant_version_id=scope.assistant_version_id,
                modes=list(scope.modes),
                collection_ids=list(scope.collection_ids),
                source_ids=list(scope.source_ids),
                adjustable=EffectiveKnowledgeScopeAdjustableResponse(
                    modes=False, collection_ids=False, source_ids=False
                ),
            )
            if scope is not None
            else None
        ),
    )


def _session_list_to_response(page: SessionPage) -> ChatSessionListResponse:
    return ChatSessionListResponse(
        items=[_session_to_response(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


def _citation_to_response(view: CitationView) -> CitationResponse:
    # Defense in depth: even if a caller hands this serializer a stale/unredacted
    # view, revoked citations never disclose media seek/speaker provenance.
    disclose_media = not view.redacted
    return CitationResponse(
        id=view.id,
        document_id=view.document_id,
        document_name=view.document_name,
        chunk_id=view.chunk_id,
        snippet=view.snippet,
        char_start=view.char_start,
        char_end=view.char_end,
        time_start_ms=view.time_start_ms if disclose_media else None,
        time_end_ms=view.time_end_ms if disclose_media else None,
        transcript_segment_id=view.transcript_segment_id if disclose_media else None,
        speaker_id=view.speaker_id if disclose_media else None,
        speaker_name=view.speaker_name if disclose_media else None,
        score=view.score,
        handle=view.handle,
        redacted=view.redacted,
    )


def _tool_invocation_to_response(inv: ToolInvocation) -> MessageToolInvocationResponse:
    return MessageToolInvocationResponse(
        id=inv.id,
        tool_name=inv.tool_name,
        ok=inv.ok,
        error=inv.error,
        result_summary=inv.result_summary,
        duration_ms=inv.duration_ms,
        created_at=inv.created_at,
    )


def _question_to_response(
    question: AskUserQuestion | None,
) -> AskUserQuestionResponse | None:
    if question is None:
        return None
    return AskUserQuestionResponse(
        question=question.question,
        options=[
            AskUserOptionResponse(label=o.label, description=o.description)
            for o in question.options
        ],
        allow_free_text=question.allow_free_text,
    )


def _message_to_response(view: MessageView) -> MessageResponse:
    m = view.message
    return MessageResponse(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        model=m.model,
        citations=[_citation_to_response(c) for c in view.citations],
        web_citations=[
            WebCitationResponse(
                id=c.id, handle=c.handle, url=c.url, title=c.title, snippet=c.snippet
            )
            for c in view.web_citations
        ],
        tool_invocations=[_tool_invocation_to_response(t) for t in view.tool_invocations],
        question=_question_to_response(m.question),
        created_at=m.created_at,
    )


def _message_list_to_response(page: MessagePage) -> MessageListResponse:
    return MessageListResponse(
        items=[_message_to_response(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


def _bare_message_to_response(message: Message) -> MessageResponse:
    """The persisted user message for the 202 body (no citations/tools yet)."""
    return MessageResponse(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        content=message.content,
        model=message.model,
        citations=[],
        tool_invocations=[],
        question=_question_to_response(message.question),
        created_at=message.created_at,
    )


def _to_chat_messages(history: tuple[Message, ...]) -> list[ChatMessage]:
    """Project persisted history into gateway chat messages for the prompt.

    System turns are skipped (the runtime prepends its own grounded system
    prompt); user/assistant turns carry through as conversation context.
    """
    role_map = {MessageRole.USER: Role.USER, MessageRole.ASSISTANT: Role.ASSISTANT}
    out: list[ChatMessage] = []
    for m in history:
        role = role_map.get(m.role)
        if role is None:
            continue
        out.append(
            ChatMessage(
                role=role,
                content=m.content,
                source_document_ids=m.source_document_ids if role is Role.ASSISTANT else (),
            )
        )
    return out


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    request: Request,
    make_audit_sink: AuditSinkFactory,
) -> ChatService:
    """Assemble the per-request chat service.

    Request correlation plus the canonical durable recorder are mandatory at
    construction: every authenticated direct-resource guard owns its denial
    audit, including the six session/message 404 paths (INV-1/INV-2/INV-6).
    """
    lifecycle = SandboxSessionService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        runner=HttpSandboxRunner(settings.sandbox_runner_url, token=settings.sandbox_runner_token),
        settings=settings,
        denials=authenticated_denial_context(
            make_audit_sink, tenant_id=tenant_id, principal=principal, request=request
        ),
    )
    return ChatService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        settings=settings,
        sandbox_lifecycle=lifecycle,
        audit=make_audit_sink(tenant_id),
        denials=authenticated_denial_context(
            make_audit_sink, tenant_id=tenant_id, principal=principal, request=request
        ),
    )


def _build_sandbox_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    request: Request,
    make_audit_sink: AuditSinkFactory,
) -> SandboxSessionService:
    """Assemble the per-request sandbox lifecycle service.

    Every lifecycle route receives the tenant-bound principal, canonical denial
    capability, and request correlation/origin in one mandatory context.
    """
    return SandboxSessionService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        runner=HttpSandboxRunner(settings.sandbox_runner_url, token=settings.sandbox_runner_token),
        settings=settings,
        denials=authenticated_denial_context(
            make_audit_sink, tenant_id=tenant_id, principal=principal, request=request
        ),
    )


def _sandbox_response(value: SandboxSession | None, *, enabled: bool) -> SandboxSessionResponse:
    if value is None:
        return SandboxSessionResponse(status="not_created", enabled=enabled)
    return SandboxSessionResponse(
        status=value.status.value,
        enabled=enabled,
        sandbox_session_id=value.id,
        generation=value.generation,
        image_digest=value.image_digest,
        created_at=value.created_at,
        last_used_at=value.last_used_at,
        closed_at=value.closed_at,
    )


# --- Session routes ---------------------------------------------------------


@router.get("/sessions", response_model=ChatSessionListResponse)
async def list_sessions(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ChatSessionListResponse:
    """List the caller's own chat sessions (cursor-paginated, newest first)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    page = await service.list_sessions(cursor=cursor, limit=limit)
    return _session_list_to_response(page)


@router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: ChatSessionCreate,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> ChatSessionResponse:
    """Create a chat session owned by the caller (unknown model → 422)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    view = await service.create_session(
        title=body.title, model=body.model, assistant_id=body.assistant_id
    )
    await session.commit()
    return _session_to_response(view)


@router.get(
    "/sessions/{session_id}",
    response_model=ChatSessionResponse,
)
async def get_session(
    session_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> ChatSessionResponse:
    """Get one of the caller's sessions; not visible → 404 (INV-1/INV-2)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    view = await service.get_session(session_id)
    if view is None:
        raise NotFoundError("Chat session not found.")
    return _session_to_response(view)


@router.get(
    "/sessions/{session_id}/usage",
    response_model=SessionUsageResponse,
    response_model_exclude_none=True,
)
async def get_session_usage(
    session_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> SessionUsageResponse:
    """The session's token/context accounting (spec 0007 #429); not visible → 404.

    Powers the conversation context meter: ``totals`` is the session-wide SUM
    over every llm_usage scope (#409 — answer scopes plus message-less
    failover/salvage and suggestions rows), while ``last`` is only the most
    recent MESSAGE-BEARING answer scope (context-meter semantics, not that
    answer's full spend; ADR-0016 §2.6). Plus the input-token budget the
    assembler grants the session's model (the same formula answers are
    assembled under — never a parallel approximation).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    view = await service.session_usage(session_id)
    if view is None:
        raise NotFoundError("Chat session not found.")
    return SessionUsageResponse(
        model=view.model,
        totals=SessionUsageTotalsResponse(
            answers=view.totals.answers,
            prompt_tokens=view.totals.prompt_tokens,
            completion_tokens=view.totals.completion_tokens,
            total_tokens=view.totals.total_tokens,
            cached_prompt_tokens=view.totals.cached_prompt_tokens,
            cache_write_tokens=view.totals.cache_write_tokens,
        ),
        last=(
            SessionUsageLastResponse(
                prompt_tokens=view.last.prompt_tokens,
                completion_tokens=view.last.completion_tokens,
                total_tokens=view.last.total_tokens,
                cached_prompt_tokens=view.last.cached_prompt_tokens,
                cache_write_tokens=view.last.cache_write_tokens,
                context_prompt_tokens=view.last.context_prompt_tokens,
            )
            if view.last is not None
            else None
        ),
        input_budget_tokens=view.input_budget_tokens,
        window_known=view.window_known,
    )


@router.patch(
    "/sessions/{session_id}",
    response_model=ChatSessionResponse,
)
async def update_session(
    session_id: UUID,
    body: ChatSessionUpdate,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> ChatSessionResponse:
    """Rename / re-model one of the caller's sessions; not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    view = await service.update_session(session_id, title=body.title, model=body.model)
    if view is None:
        raise NotFoundError("Chat session not found.")
    await session.commit()
    return _session_to_response(view)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Delete one of the caller's sessions (cascades); not visible → 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    deleted = await service.delete_session(session_id)
    if not deleted:
        raise NotFoundError("Chat session not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get(
    "/sessions/{session_id}/sandbox",
    response_model=SandboxSessionResponse,
    response_model_exclude_none=True,
)
async def get_sandbox_session(
    session_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> SandboxSessionResponse:
    """Inspect this visible chat's reusable sandbox state."""
    service = _build_sandbox_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    value = await service.get(session_id)
    return _sandbox_response(value, enabled=await service.is_enabled())


@router.post(
    "/sessions/{session_id}/sandbox/reset",
    response_model=SandboxSessionResponse,
    response_model_exclude_none=True,
)
async def reset_sandbox_session(
    session_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> SandboxSessionResponse:
    """Destroy prior state and start a clean sandbox generation."""
    service = _build_sandbox_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    try:
        value = await service.reset(session_id)
    except DependencyError:
        # The service already advanced the durable generation and marked it error.
        # Preserve that recovery signal even when the runner failed after destroying
        # the old container; rolling back would falsely report the old state active.
        await session.commit()
        raise
    except SandboxDisabledError:
        # The refusal IS the record (#510): `AuditSink.emit` flushes but never
        # commits — the caller owns the transaction — so without this the
        # `permission.denied` row unwinds with the request and a refused caller
        # leaves no trace, which is the entirety of what #510 fixes.
        await session.commit()
        raise
    await session.commit()
    return _sandbox_response(value, enabled=True)


@router.delete("/sessions/{session_id}/sandbox", status_code=status.HTTP_204_NO_CONTENT)
async def close_sandbox_session(
    session_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Idempotently close and destroy a visible chat's reusable sandbox."""
    service = _build_sandbox_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    await service.close(session_id)
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


# --- Message routes ---------------------------------------------------------


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessageListResponse,
    response_model_exclude_none=True,
)
async def list_messages(
    session_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MessageListResponse:
    """Message history for a session (oldest → newest), citations on assistant turns."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    page = await service.list_messages(session_id, cursor=cursor, limit=limit)
    if page is None:
        raise NotFoundError("Chat session not found.")
    # This read AUDITS when it withholds a citation the caller may no longer see
    # (#536). `AuditSink.emit` flushes but does not commit, so an audited read
    # has to close its own transaction or the row rolls back when the
    # request-scoped session ends — the same reason `search` and
    # `document.viewed` commit. The redaction happens either way; the INV-6
    # record is what is lost.
    await session.commit()
    return _message_list_to_response(page)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    response_model_exclude_none=True,
)
async def send_message(
    session_id: UUID,
    body: SendMessageRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    backplane: BackplaneDep,
    make_audit_sink: AuditSinkFactory,
) -> SendMessageResponse:
    """Persist the user message (202) and kick off the streamed grounded answer.

    The unknown-model check (422) and the not-visible check (404) happen *before*
    anything is persisted. On success the user message is committed and the
    agentic answer runtime is launched as a detached, tracked ``asyncio.Task``
    (issue #156) that streams over the WS backplane keyed by the returned
    ``stream_id`` — off the request cycle, so a slow/hung answer neither delays
    the 202 nor blocks graceful shutdown.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        request=request,
        make_audit_sink=make_audit_sink,
    )
    result = await service.send_message(
        session_id, content=body.content, model=body.model, backplane=backplane
    )
    if result is None:
        raise NotFoundError("Chat session not found.")
    await session.commit()

    _schedule_answer(
        backplane=backplane,
        principal=principal,
        request=request,
        session_id=session_id,
        result=result,
        collection_ids=body.collection_ids,
        document_ids=body.document_ids,
        settings=settings,
    )
    return SendMessageResponse(
        message=_bare_message_to_response(result.user_message),
        stream_id=result.stream_id,
    )


def _schedule_answer(
    *,
    backplane: Backplane,
    principal: CurrentUser,
    request: Request,
    session_id: UUID,
    result: SendResult,
    collection_ids: list[UUID] | None,
    document_ids: list[UUID] | None = None,
    settings: Settings,
) -> None:
    """Launch the answer runtime as a tracked task detached from the response.

    The runtime owns its **own** DB session (the request session is closed once
    the response is returned), so it is built with the sessionmaker, not the
    request session. It streams over the injected backplane (overridable in
    tests). ``default_max_tool_turns`` is the configured system default budget;
    the runtime applies the tenant's per-tenant override over it (issue #148).

    Rather than a Starlette ``BackgroundTask`` (which runs inside the response
    cycle and so keeps the request — and graceful shutdown — alive until it
    finishes, issue #156), the producer is scheduled as an ``asyncio.Task``
    registered in the app's process-wide ``answer_tasks`` set. The lifespan
    teardown cancels and drains that set under a bounded grace window; the
    ``add_done_callback`` discards a finished task so the set self-empties.

    The #231 ``run_python`` sandbox seam is wired via a factory the runtime only
    invokes when a run's allow-list actually offers ``run_python`` (an admin/
    assistant-gated, off-by-default tool). The deny-by-default enable/package gate lives
    inside the sandbox service the seam composes (``SANDBOX_ENABLED`` / the #233
    per-tenant enable), so a disabled tenant's run is refused there and surfaces as a
    typed ``ok=False`` tool result — never an exception.
    """
    request_id = extract_request_id(request) or "unknown"
    source_ip = request.client.host if request.client else "unknown"
    # The ONE composition (#576): model route, MCP, sandbox, artifact
    # writer, context + interactive deadlines all wired there.
    # `runtime_factory=ChatRuntime` preserves the module-level test seam.
    runtime = build_chat_runtime(
        mode="interactive",
        principal=principal,
        backplane=backplane,
        settings=settings,
        sessionmaker=get_sessionmaker(),
        gateway=get_llm_gateway(),
        request_id=request_id,
        source_ip=source_ip,
        options=ChatCompositionOptions(runtime_factory=ChatRuntime),
    )
    history = _to_chat_messages(result.history)

    async def _produce() -> None:
        answered = await runtime.run(
            stream_id=result.stream_id,
            session_id=session_id,
            question=result.user_message.content,
            model=result.model,
            history=history,
            collection_ids=collection_ids,
            document_ids=document_ids,
            assistant_config=result.assistant_config,
            custom_instructions=result.custom_instructions,
            summary=result.summary,
            evidence=result.evidence,
            mentioned_documents=result.mentioned_documents,
            compaction_cursor=result.compaction_cursor,
        )
        # Rolling-summary refresh (#416, ADR-0016 §3.2): enqueued AFTER a
        # successful answer, off the event loop (the #401 lesson — a Celery
        # send is blocking I/O), and best-effort: an enqueue failure must
        # never fail an answer that already streamed (AC-4). The task itself
        # no-ops when the uncovered tail is shorter than the verbatim window.
        if settings.chat_summary_enabled and answered:
            # Only a SUCCESSFUL answer feeds memory (#446 finding 7): an
            # errored turn must not fold an unanswered question into the
            # summary. The enqueue helper is owned by ``tasks/`` (api/ never
            # speaks to the broker directly).
            try:
                await asyncio.to_thread(enqueue_summarize, principal.tenant_id, session_id)
            except Exception as exc:  # noqa: BLE001 — nicety, never the answer
                log.warning("chat.summarize_enqueue_failed", error_type=type(exc).__name__)

    tasks: set[asyncio.Task[None]] = request.app.state.answer_tasks
    task = asyncio.create_task(_produce())
    tasks.add(task)
    task.add_done_callback(tasks.discard)
