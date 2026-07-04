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
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    BackplaneDep,
    CurrentTenant,
    CurrentUser,
    DbSession,
    SettingsDep,
    extract_request_id,
    get_llm_gateway,
)
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.db.repositories import AuditEventRepository, CitationView
from app.db.session import get_sessionmaker
from app.domain.entities import Message, MessageRole
from app.domain.llm import ChatMessage, Role
from app.realtime.backplane import Backplane
from app.sandbox.runner import HttpSandboxRunner
from app.sandbox.tool_runner import ChatSandboxToolRunner
from app.services.audit import AuditSink
from app.services.chat_runtime import ChatRuntime, SandboxContext
from app.services.chat_service import (
    ChatService,
    MessagePage,
    MessageView,
    SendResult,
    SessionPage,
    SessionView,
)
from app.services.mcp_servers_service import build_mcp_servers_service
from app.services.provider_models import ModelRouteResolver, build_model_route_resolver
from app.services.tools.types import SandboxToolRunner, ToolDefinition
from app.storage import ObjectStore

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
    score: float | None = None


class MessageResponse(BaseModel):
    """``#/components/schemas/Message``."""

    model_config = {"extra": "forbid"}

    id: UUID
    session_id: UUID
    role: MessageRole
    content: str
    model: str | None = None
    citations: list[CitationResponse]
    created_at: datetime


class MessageListResponse(BaseModel):
    """``#/components/schemas/MessageList``."""

    model_config = {"extra": "forbid"}

    items: list[MessageResponse]
    next_cursor: str | None = None


class SendMessageRequest(BaseModel):
    """``#/components/schemas/SendMessageRequest``."""

    model_config = {"extra": "forbid"}

    content: str = Field(min_length=1)
    model: str | None = None
    collection_ids: list[UUID] | None = None


class SendMessageResponse(BaseModel):
    """``#/components/schemas/SendMessageResponse`` — the persisted user msg + id."""

    model_config = {"extra": "forbid"}

    message: MessageResponse
    stream_id: str


# --- Serialisation helpers --------------------------------------------------


def _session_to_response(view: SessionView) -> ChatSessionResponse:
    s = view.session
    return ChatSessionResponse(
        id=s.id,
        title=s.title,
        model=s.model,
        owner_id=s.owner_id,
        message_count=view.message_count,
        created_at=s.created_at,
        updated_at=s.updated_at,
    )


def _session_list_to_response(page: SessionPage) -> ChatSessionListResponse:
    return ChatSessionListResponse(
        items=[_session_to_response(v) for v in page.items],
        next_cursor=page.next_cursor,
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


def _message_to_response(view: MessageView) -> MessageResponse:
    m = view.message
    return MessageResponse(
        id=m.id,
        session_id=m.session_id,
        role=m.role,
        content=m.content,
        model=m.model,
        citations=[_citation_to_response(c) for c in view.citations],
        created_at=m.created_at,
    )


def _message_list_to_response(page: MessagePage) -> MessageListResponse:
    return MessageListResponse(
        items=[_message_to_response(v) for v in page.items],
        next_cursor=page.next_cursor,
    )


def _bare_message_to_response(message: Message) -> MessageResponse:
    """The persisted user message for the 202 body (no citations yet)."""
    return MessageResponse(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        content=message.content,
        model=message.model,
        citations=[],
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
        out.append(ChatMessage(role=role, content=m.content))
    return out


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> ChatService:
    return ChatService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        settings=settings,
    )


# --- Session routes ---------------------------------------------------------


@router.get("/sessions", response_model=ChatSessionListResponse, response_model_exclude_none=True)
async def list_sessions(
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ChatSessionListResponse:
    """List the caller's own chat sessions (cursor-paginated, newest first)."""
    service = _build_service(
        session=session, principal=principal, tenant_id=tenant_id, settings=settings
    )
    page = await service.list_sessions(cursor=cursor, limit=limit)
    return _session_list_to_response(page)


@router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_session(
    body: ChatSessionCreate,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> ChatSessionResponse:
    """Create a chat session owned by the caller (unknown model → 422)."""
    service = _build_service(
        session=session, principal=principal, tenant_id=tenant_id, settings=settings
    )
    view = await service.create_session(
        title=body.title, model=body.model, assistant_id=body.assistant_id
    )
    await session.commit()
    return _session_to_response(view)


@router.get(
    "/sessions/{session_id}",
    response_model=ChatSessionResponse,
    response_model_exclude_none=True,
)
async def get_session(
    session_id: UUID,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> ChatSessionResponse:
    """Get one of the caller's sessions; not visible → 404 (INV-1/INV-2)."""
    service = _build_service(
        session=session, principal=principal, tenant_id=tenant_id, settings=settings
    )
    view = await service.get_session(session_id)
    if view is None:
        raise NotFoundError("Chat session not found.")
    return _session_to_response(view)


@router.patch(
    "/sessions/{session_id}",
    response_model=ChatSessionResponse,
    response_model_exclude_none=True,
)
async def update_session(
    session_id: UUID,
    body: ChatSessionUpdate,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> ChatSessionResponse:
    """Rename / re-model one of the caller's sessions; not visible → 404."""
    service = _build_service(
        session=session, principal=principal, tenant_id=tenant_id, settings=settings
    )
    view = await service.update_session(session_id, title=body.title, model=body.model)
    if view is None:
        raise NotFoundError("Chat session not found.")
    await session.commit()
    return _session_to_response(view)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
) -> Response:
    """Delete one of the caller's sessions (cascades); not visible → 404."""
    service = _build_service(
        session=session, principal=principal, tenant_id=tenant_id, settings=settings
    )
    deleted = await service.delete_session(session_id)
    if not deleted:
        raise NotFoundError("Chat session not found.")
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
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> MessageListResponse:
    """Message history for a session (oldest → newest), citations on assistant turns."""
    service = _build_service(
        session=session, principal=principal, tenant_id=tenant_id, settings=settings
    )
    page = await service.list_messages(session_id, cursor=cursor, limit=limit)
    if page is None:
        raise NotFoundError("Chat session not found.")
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
        session=session, principal=principal, tenant_id=tenant_id, settings=settings
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
    assistant-gated, off-by-default tool). The deny-by-default admin/quota gate lives
    inside the sandbox service the seam composes (``SANDBOX_ENABLED`` / the #233
    per-tenant enable), so a disabled tenant's run is refused there and surfaces as a
    typed ``ok=False`` tool result — never an exception.
    """
    request_id = extract_request_id(request) or "unknown"
    source_ip = request.client.host if request.client else "unknown"
    runtime = ChatRuntime(
        sessionmaker=get_sessionmaker(),
        gateway=get_llm_gateway(),
        backplane=backplane,
        principal=principal,
        request_id=request_id,
        source_ip=source_ip,
        default_max_tool_turns=settings.chat_max_tool_turns,
        sandbox_factory=_build_sandbox_factory(
            principal=principal,
            backplane=backplane,
            settings=settings,
            request_id=request_id,
            source_ip=source_ip,
        ),
        mcp_tools_factory=_build_mcp_tools_factory(
            principal=principal,
            settings=settings,
            request_id=request_id,
            source_ip=source_ip,
        ),
        model_route_resolver=_build_model_route_resolver(
            principal=principal,
            settings=settings,
            request_id=request_id,
            source_ip=source_ip,
        ),
    )
    history = _to_chat_messages(result.history)

    async def _produce() -> None:
        await runtime.run(
            stream_id=result.stream_id,
            session_id=session_id,
            question=result.user_message.content,
            model=result.model,
            history=history,
            collection_ids=collection_ids,
            assistant_config=result.assistant_config,
        )

    tasks: set[asyncio.Task[None]] = request.app.state.answer_tasks
    task = asyncio.create_task(_produce())
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _build_sandbox_factory(
    *,
    principal: CurrentUser,
    backplane: Backplane,
    settings: Settings,
    request_id: str,
    source_ip: str,
) -> Callable[[SandboxContext], SandboxToolRunner | None]:
    """Build the live ``run_python`` seam factory (issue #231).

    Returns a closure the runtime calls **only** when a run's allow-list offers
    ``run_python``; it composes the live :class:`HttpSandboxRunner` (the worker's
    only container-engine surface — an internal HTTP hop to the dedicated runner, no
    Docker socket) and the object store into a :class:`ChatSandboxToolRunner` scoped
    to the streaming principal (tenant + owner from the token, never model input).
    The deny-by-default admin/quota gate is inside the sandbox service the seam
    composes, so this factory does not gate — it wires; a disabled tenant's run is
    refused downstream and surfaces as a typed ``ok=False`` result.
    """

    def _factory(ctx: SandboxContext) -> SandboxToolRunner | None:
        return ChatSandboxToolRunner(
            session=ctx.session,
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            runner=HttpSandboxRunner(settings.sandbox_runner_url),
            object_store=ObjectStore(settings),
            settings=settings,
            backplane=backplane,
            stream_id=ctx.stream_id,
            request_id=request_id,
            source_ip=source_ip,
            next_seq=ctx.next_seq,
            session_id=ctx.session_id,
            message_id=ctx.message_id,
        )

    return _factory


def _build_mcp_tools_factory(
    *,
    principal: CurrentUser,
    settings: Settings,
    request_id: str,
    source_ip: str,
) -> Callable[[AsyncSession], Awaitable[dict[str, ToolDefinition]]]:
    """Build the live MCP-tools resolver the runtime calls per answer (issue #227).

    Returns a coroutine the runtime invokes **only** when a run's allow-list names
    at least one ``mcp:*`` tool. It builds the per-tenant :class:`McpServersService`
    over the runtime's own session (the request session is gone by then) — scoped to
    the streaming principal (tenant + owner + roles from the token, never model
    input) — and resolves its registered+enabled MCP tools into governed CC-A
    :class:`ToolDefinition`s. The SSRF guard, per-tenant rate limit, and CC-C auth
    resolution all live inside that service's client, so the resolved handlers invoke
    upstream through the same governed egress the ``/mcp-servers`` surface uses.
    """

    async def _factory(session: AsyncSession) -> dict[str, ToolDefinition]:
        audit = AuditSink(AuditEventRepository(session, principal.tenant_id))
        service = build_mcp_servers_service(
            session,
            settings=settings,
            tenant_id=principal.tenant_id,
            owner_id=principal.user_id,
            roles=principal.roles,
            audit=audit,
            request_id=request_id,
            source_ip=source_ip,
        )
        return await service.resolve_run_tools()

    return _factory


def _build_model_route_resolver(
    *,
    principal: CurrentUser,
    settings: Settings,
    request_id: str,
    source_ip: str,
) -> ModelRouteResolver:
    """Wire the live per-tenant model-route resolver the runtime calls (PR 2a).

    A thin adapter over the ``services`` factory
    (:func:`~app.services.provider_models.build_model_route_resolver`) scoped to the
    streaming principal (tenant + owner + roles from the token, never model input).
    The resolver decrypts a per-tenant provider's key **inside** ``services/`` (the
    CC-C vault never enters ``api/`` — the architecture test forbids a router
    importing the secrets service or naming ``get_secret_plaintext``); this router
    only builds it and hands it to the runtime, which invokes it once per answer.
    """
    return build_model_route_resolver(
        settings=settings,
        tenant_id=principal.tenant_id,
        owner_id=principal.user_id,
        roles=principal.roles,
        request_id=request_id,
        source_ip=source_ip,
    )
