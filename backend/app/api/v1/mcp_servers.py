"""MCP server routes — register / test / list / update / delete (#226, ADR-0012 §5).

Contract-first: shapes match ``contracts/openapi.yaml`` (``McpServerCreate`` /
``McpServerUpdate`` → ``McpServer``; ``McpServerList`` with cursor pagination;
``McpToolList`` for discovered tools; ``201`` register, ``200`` get/patch/test,
``204`` delete). This module exposes a module-level ``router`` and is
**auto-discovered** (``api/v1/__init__.py`` scans for it) — no edit to the
aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all
orchestration (ownership/tenancy enforcement, transport + SSRF validation, the
CC-C credential store/resolve, the health/discovery probe, the audit, the cursor
codec) lives in ``services.mcp_servers_service``; this layer only (de)serialises
and threads correlation context.

**The credential is never returned (CC-C AC-3 / ADR-0012 §5).** No response shape
here carries the stored auth value — only the masked ``secret_hint``. The
write-only ``auth`` is accepted on register/update and stored envelope-encrypted
via CC-C; a negative test asserts no endpoint echoes it back.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A server's
``tenant_id``/``owner_id`` come from the resolved principal (never request input).
A server in another tenant, or owned by another user (and the caller is not an
admin), is reported as **404** (existence non-disclosure): the service returns
``None`` and this router maps that to ``NotFoundError``, never 403. Every route
requires the bearer token; unauthenticated → 401 (INV-4).

**SSRF (ADR-0012 §4).** ``POST``/``PATCH`` validate the endpoint (https + the
shared range check) via the service; a non-https or blocked endpoint raises a
typed ``ValidationError`` → **422** with a Problem ``code`` (``endpoint_scheme`` /
``endpoint_blocked``); an unsupported / ``stdio`` transport is
``unsupported_transport`` → **422**. Mapped by the global exception handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    SettingsDep,
    authenticated_denial_context,
    extract_request_id,
)
from app.core.errors import NotFoundError
from app.domain.entities import McpServer
from app.services.mcp_servers_service import (
    McpAuthInput,
    McpServerPage,
    McpServersService,
    McpToolView,
    build_mcp_servers_service,
)

router = APIRouter(prefix="/mcp-servers", tags=["mcp-servers"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class McpServerAuthRequest(BaseModel):
    """``#/components/schemas/McpServerAuth`` — write-only auth material.

    ``writeOnly`` in the contract: accepted on register/update, never echoed. A
    ``bearer`` token or a custom ``header`` (requires ``header_name``); exactly one
    form per registration.
    """

    model_config = {"extra": "forbid"}

    type: Literal["bearer", "header"]
    value: str = Field(min_length=1)
    header_name: str | None = None

    @model_validator(mode="after")
    def _require_header_name(self) -> McpServerAuthRequest:
        if self.type == "header" and not (self.header_name and self.header_name.strip()):
            raise ValueError("header_name is required when type is 'header'")
        return self

    def to_input(self) -> McpAuthInput:
        return McpAuthInput(kind=self.type, value=self.value, header_name=self.header_name)


class McpServerResponse(BaseModel):
    """``#/components/schemas/McpServer`` — the wire projection of a server.

    Carries only the masked ``secret_hint`` — **never** the credential value
    (CC-C AC-3 / ADR-0012 §5). The ``auth_secret_ref`` is a server-internal detail
    and is not exposed.
    """

    model_config = {"extra": "forbid"}

    id: UUID
    name: str
    transport: str
    endpoint_url: str
    enabled: bool
    status: str
    last_health_at: datetime | None = None
    last_error: str | None = None
    discovered_tool_count: int
    secret_hint: str | None = None
    owner_id: UUID
    created_at: datetime
    updated_at: datetime


class McpServerListResponse(BaseModel):
    """``#/components/schemas/McpServerList`` — a cursor-paginated page."""

    model_config = {"extra": "forbid"}

    items: list[McpServerResponse]
    next_cursor: str | None = None


class McpServerCreateRequest(BaseModel):
    """``#/components/schemas/McpServerCreate`` — register a remote MCP server."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=200)
    transport: str
    endpoint_url: str
    auth: McpServerAuthRequest | None = None


class McpServerUpdateRequest(BaseModel):
    """``#/components/schemas/McpServerUpdate`` — any subset of fields; at least one.

    ``auth`` is a tri-state on the wire: absent = leave the credential unchanged,
    an object = rotate it, ``null`` = clear it. Pydantic collapses "absent" and
    "explicit null" to ``None``, so ``__pydantic_fields_set__`` distinguishes them
    (``auth`` in the set + ``None`` ⇒ clear).
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=200)
    transport: str | None = None
    endpoint_url: str | None = None
    enabled: bool | None = None
    auth: McpServerAuthRequest | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> McpServerUpdateRequest:
        if not self.__pydantic_fields_set__:
            raise ValueError("at least one field must be provided")
        return self


class McpToolResponse(BaseModel):
    """``#/components/schemas/McpTool`` — one discovered tool (namespaced + tiered)."""

    model_config = {"extra": "forbid"}

    name: str
    raw_name: str
    description: str | None = None
    input_schema: dict[str, object]
    risk_tier: str
    read_only: bool


class McpToolListResponse(BaseModel):
    """``#/components/schemas/McpToolList`` — a page of discovered tools."""

    model_config = {"extra": "forbid"}

    items: list[McpToolResponse]
    next_cursor: str | None = None


# --- Serialisation helpers --------------------------------------------------


def _to_response(server: McpServer) -> McpServerResponse:
    return McpServerResponse(
        id=server.id,
        name=server.name,
        transport=server.transport,
        endpoint_url=server.endpoint_url,
        enabled=server.enabled,
        status=server.status.value,
        last_health_at=server.last_health_at,
        last_error=server.last_error,
        discovered_tool_count=len(server.discovered_tools),
        secret_hint=server.secret_hint,
        owner_id=server.owner_id,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def _to_list_response(page: McpServerPage) -> McpServerListResponse:
    return McpServerListResponse(
        items=[_to_response(s) for s in page.items],
        next_cursor=page.next_cursor,
    )


def _to_tool_response(view: McpToolView) -> McpToolResponse:
    return McpToolResponse(
        name=view.name,
        raw_name=view.raw_name,
        description=view.description,
        input_schema=view.input_schema,
        risk_tier=view.risk_tier,
        read_only=view.read_only,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
    request: Request,
) -> McpServersService:
    """Assemble the per-request service from the identity + settings + audit seams.

    Delegates to ``build_mcp_servers_service`` (a ``services/`` factory), which owns
    the CC-C secrets service (the sole cipher importer — so no ``api/`` module ever
    touches plaintext, the AC-3 architecture rule) and the per-tenant MCP egress
    rate limiter. The router stays a thin (de)serialise + one-service call.
    """
    return build_mcp_servers_service(
        session,
        settings=settings,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        roles=principal.roles,
        audit=make_audit_sink(tenant_id),
        denials=authenticated_denial_context(
            make_audit_sink,
            tenant_id=tenant_id,
            principal=principal,
            request=request,
        ),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=McpServerListResponse, response_model_exclude_none=True)
async def list_mcp_servers(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> McpServerListResponse:
    """List the caller's own registered MCP servers (cursor-paginated, newest first)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    page = await service.list_page(cursor=cursor, limit=limit)
    return _to_list_response(page)


@router.post(
    "",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def register_mcp_server(
    body: McpServerCreateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> McpServerResponse:
    """Register a remote MCP server (status pending; secret stored write-only via CC-C).

    Validates the transport (unsupported/``stdio`` → 422) and the endpoint (https +
    SSRF → 422 on a block). The optional ``auth`` is stored envelope-encrypted via
    CC-C and never returned — the response carries only a masked ``secret_hint``.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    server = await service.register(
        name=body.name,
        transport=body.transport,
        endpoint_url=body.endpoint_url,
        auth=body.auth.to_input() if body.auth is not None else None,
    )
    await session.commit()
    return _to_response(server)


@router.get("/{server_id}", response_model=McpServerResponse, response_model_exclude_none=True)
async def get_mcp_server(
    server_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> McpServerResponse:
    """Get one of the caller's MCP servers; a non-owner / cross-tenant id is 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    server = await service.get(server_id)
    if server is None:
        raise NotFoundError("MCP server not found.")
    return _to_response(server)


@router.patch("/{server_id}", response_model=McpServerResponse, response_model_exclude_none=True)
async def update_mcp_server(
    server_id: UUID,
    body: McpServerUpdateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> McpServerResponse:
    """Update a server (rename, toggle, rotate/clear credential); else 404 / 422.

    ``auth`` is tri-state: absent = unchanged, an object = rotate, ``null`` = clear
    (the field being in the set with a ``None`` value). The credential value is
    never returned — only the updated masked ``secret_hint``.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    clear_auth = "auth" in body.__pydantic_fields_set__ and body.auth is None
    updated = await service.update(
        server_id,
        name=body.name,
        transport=body.transport,
        endpoint_url=body.endpoint_url,
        enabled=body.enabled,
        auth=body.auth.to_input() if body.auth is not None else None,
        clear_auth=clear_auth,
    )
    if updated is None:
        raise NotFoundError("MCP server not found.")
    await session.commit()
    return _to_response(updated)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> Response:
    """Delete a server + its stored credential + discovered tools; else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    deleted = await service.delete(server_id)
    if not deleted:
        raise NotFoundError("MCP server not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/{server_id}/test",
    response_model=McpServerResponse,
    response_model_exclude_none=True,
)
async def test_mcp_server(
    server_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
) -> McpServerResponse:
    """Probe health + re-discover tools; persist the outcome. Else 404.

    Both a healthy (``status: ready``) and an unreachable/erroring (``status:
    error``) result return **200** — the outcome is in ``status`` / ``last_error``,
    never the HTTP code (ADR-0012 §7). The credential is fetched in-process at
    connect time and never returned.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    server = await service.test(server_id)
    if server is None:
        raise NotFoundError("MCP server not found.")
    await session.commit()
    return _to_response(server)


@router.get(
    "/{server_id}/tools",
    response_model=McpToolListResponse,
    response_model_exclude_none=True,
)
async def list_mcp_server_tools(
    server_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    make_audit_sink: AuditSinkFactory,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> McpToolListResponse:
    """The tools discovered from this server on the last successful probe; else 404.

    Reads the persisted snapshot (no live connect); each tool carries its
    namespaced registry name + inferred risk tier (ADR-0012 §6). Pagination is a
    keyset over the in-memory snapshot (small, per-server).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        settings=settings,
        make_audit_sink=make_audit_sink,
        request=request,
    )
    tools = await service.list_tools(server_id)
    if tools is None:
        raise NotFoundError("MCP server not found.")
    return McpToolListResponse(items=[_to_tool_response(t) for t in tools], next_cursor=None)
