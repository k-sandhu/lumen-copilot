"""Sources routes — list / add / sync / delete (#20, ADR-0009 §5).

Contract-first: shapes match ``contracts/openapi.yaml`` (``SourceCreate`` →
``Source``; ``SourceList`` with cursor pagination; ``204`` delete; ``200``/``202``
sync). This module exposes a module-level ``router`` and is **auto-discovered**
(``api/v1/__init__.py`` scans for it) — no edit to the aggregator.

Routers validate in → call **one** service → shape out (ADR-0004): all
orchestration (ownership/tenancy enforcement, connector resolution + SSRF check,
audit, the sync enqueue, the cursor codec) lives in ``services.sources_service``;
this layer only (de)serialises and threads correlation context.

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2).** A source's
``tenant_id``/``owner_id`` come from the resolved principal (the token, never
request input). A source in another tenant or owned by another user is reported
as **404** (existence non-disclosure): the service returns ``None``/``False`` and
this router maps that to ``NotFoundError``, never 403. Every route requires the
bearer token; unauthenticated → 401 via ``current_user`` (INV-4).

**SSRF (ADR-0009 §3).** ``POST /sources`` validates + SSRF-checks the URL via the
connector; a blocked or invalid URL raises a typed ``ConnectorConfigError`` →
**422** with a Problem ``code`` (``url_blocked`` for an SSRF rejection). Mapped by
the global exception handler (the error is an ``AppError`` subclass), so this
router does not special-case it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, model_serializer

from app.api.deps import (
    AuditSinkFactory,
    CurrentTenant,
    CurrentUser,
    DbSession,
    OAuthStateStoreDep,
    OAuthTokenHttpDep,
    ObjectStoreDep,
    SettingsDep,
    extract_request_id,
)
from app.connectors.oauth import REAUTHORIZE_REQUIRED
from app.core.errors import NotFoundError
from app.domain.entities import Source, SourceStatus, WebSourceMode
from app.services.connector_oauth_service import ConnectorOAuthService
from app.services.sources_service import SourcePage, SourcesService

router = APIRouter(prefix="/sources", tags=["sources"])


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class SourceConfigResponse(BaseModel):
    """``#/components/schemas/WebSourceConfig`` — the web connector's ``{url, mode}``.

    ``additionalProperties: false`` in the contract: the internal ``collection_id``
    the service stores on the source config is **not** exposed — only ``url`` and
    ``mode`` are projected onto the wire. The frozen contract marks **both**
    ``url`` and ``mode`` required, so ``mode`` is non-null on every response: it is
    populated from a URL heuristic at creation and refined during sync.
    """

    model_config = {"extra": "forbid"}

    url: str
    mode: WebSourceMode


class GdriveSourceConfigResponse(BaseModel):
    """``#/components/schemas/GdriveSourceConfig`` — the mode-discriminated variants.

    The contract's variants are **closed** shapes: a ``my_drive`` config must not
    even carry a ``folder_id``/``drive_id`` key, so the serializer emits only the
    keys that are set (a ``null`` id would be an undeclared property under
    ``additionalProperties: false``).
    """

    model_config = {"extra": "forbid"}

    mode: str
    folder_id: str | None = None
    drive_id: str | None = None

    @model_serializer
    def _contract_shape(self) -> dict[str, str]:
        out: dict[str, str] = {"mode": self.mode}
        if self.folder_id is not None:
            out["folder_id"] = self.folder_id
        if self.drive_id is not None:
            out["drive_id"] = self.drive_id
        return out


class SourceResponse(BaseModel):
    """``#/components/schemas/WebSource`` — the (pre-0.7.0) web wire projection.

    The serializer is contract-exact: ``last_synced_at`` is required-nullable
    (always emitted), ``last_error`` is optional non-nullable (emitted only when
    set) — so this model must NOT rely on route-level ``exclude_none``.
    """

    model_config = {"extra": "forbid"}

    id: UUID
    type: Literal["web"] = "web"
    config: SourceConfigResponse
    status: SourceStatus
    indexed_count: int
    last_synced_at: datetime | None = None
    last_error: str | None = None
    owner_id: UUID
    created_at: datetime
    updated_at: datetime

    @model_serializer
    def _contract_shape(self) -> dict[str, object]:
        out: dict[str, object] = {
            "id": self.id,
            "type": self.type,
            "config": self.config,
            "status": self.status,
            "indexed_count": self.indexed_count,
            "last_synced_at": self.last_synced_at,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.last_error is not None:
            out["last_error"] = self.last_error
        return out


class GdriveSourceResponse(BaseModel):
    """``#/components/schemas/GdriveSource`` — the managed-source wire projection.

    The four health fields are contract-**required** (nullable where absence is
    a value), so the serializer always emits them; ``last_error`` stays optional
    non-nullable like the web branch.
    """

    model_config = {"extra": "forbid"}

    id: UUID
    type: Literal["gdrive"] = "gdrive"
    config: GdriveSourceConfigResponse
    status: SourceStatus
    indexed_count: int
    last_synced_at: datetime | None = None
    last_error: str | None = None
    connected_account: dict[str, str] | None = None
    acl_synced_at: datetime | None = None
    unmapped_acl_count: int | None = None
    reauthorize_required: bool = False
    owner_id: UUID
    created_at: datetime
    updated_at: datetime

    @model_serializer
    def _contract_shape(self) -> dict[str, object]:
        out: dict[str, object] = {
            "id": self.id,
            "type": self.type,
            "config": self.config,
            "status": self.status,
            "indexed_count": self.indexed_count,
            "last_synced_at": self.last_synced_at,
            "connected_account": self.connected_account,
            "acl_synced_at": self.acl_synced_at,
            "unmapped_acl_count": self.unmapped_acl_count,
            "reauthorize_required": self.reauthorize_required,
            "owner_id": self.owner_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.last_error is not None:
            out["last_error"] = self.last_error
        return out


SourceWireResponse = Annotated[
    SourceResponse | GdriveSourceResponse,
    Field(discriminator="type"),
]


class SourceListResponse(BaseModel):
    """``#/components/schemas/SourceList`` — a cursor-paginated page."""

    model_config = {"extra": "forbid"}

    items: list[SourceWireResponse]
    next_cursor: str | None = None


class SourceConnectResponse(BaseModel):
    """``#/components/schemas/SourceConnectResponse`` — the consent URL."""

    model_config = {"extra": "forbid"}

    authorization_url: str


class WebSourceCreateRequest(BaseModel):
    """``#/components/schemas/WebSourceCreate`` — ``{type: web, url}``."""

    model_config = {"extra": "forbid"}

    type: Literal["web"]
    url: str


class GdriveMyDriveConfigRequest(BaseModel):
    model_config = {"extra": "forbid"}

    mode: Literal["my_drive"]


class GdriveFolderConfigRequest(BaseModel):
    model_config = {"extra": "forbid"}

    mode: Literal["folder"]
    folder_id: str = Field(min_length=1)
    drive_id: str | None = Field(default=None, min_length=1)


class GdriveSharedDriveConfigRequest(BaseModel):
    model_config = {"extra": "forbid"}

    mode: Literal["shared_drive"]
    drive_id: str = Field(min_length=1)


GdriveConfigRequest = Annotated[
    GdriveMyDriveConfigRequest | GdriveFolderConfigRequest | GdriveSharedDriveConfigRequest,
    Field(discriminator="mode"),
]


class GdriveSourceCreateRequest(BaseModel):
    """``#/components/schemas/GdriveSourceCreate`` — ``{type: gdrive, config}``.

    The mode-discriminated config variants are **closed** (INV-8): a missing
    conditional id — or an id the mode does not take — fails validation.
    """

    model_config = {"extra": "forbid"}

    type: Literal["gdrive"]
    config: GdriveConfigRequest


SourceCreateRequest = Annotated[
    WebSourceCreateRequest | GdriveSourceCreateRequest,
    Field(discriminator="type"),
]


# --- Serialisation helpers --------------------------------------------------


def _config_response(config: dict[str, object]) -> SourceConfigResponse:
    """Project the stored config to the wire ``{url, mode}`` (drops internal keys).

    ``mode`` is contract-required (non-null): every stored web-source config gets
    a ``mode`` at creation (URL heuristic, refined during sync). As defence in
    depth — so a legacy/partial row can never emit a contract-invalid response —
    a missing or unrecognised ``mode`` falls back to ``page`` (the safe default).
    """
    url = config.get("url")
    mode_raw = config.get("mode")
    try:
        mode = WebSourceMode(mode_raw) if isinstance(mode_raw, str) else WebSourceMode.PAGE
    except ValueError:
        mode = WebSourceMode.PAGE
    return SourceConfigResponse(url=str(url) if url is not None else "", mode=mode)


def _gdrive_config_response(config: dict[str, object]) -> GdriveSourceConfigResponse:
    """Project a stored gdrive config to the closed wire variant (drops internals)."""
    mode = config.get("mode")
    folder_id = config.get("folder_id")
    drive_id = config.get("drive_id")
    return GdriveSourceConfigResponse(
        mode=str(mode) if isinstance(mode, str) else "my_drive",
        folder_id=folder_id if isinstance(folder_id, str) else None,
        drive_id=drive_id if isinstance(drive_id, str) else None,
    )


def _connected_account_response(source: Source) -> dict[str, str] | None:
    """The ``ConnectedAccount`` wire shape ({email}) — or null (never tokens)."""
    account = source.connected_account
    if not account:
        return None
    email = account.get("email")
    return {"email": str(email)} if isinstance(email, str) and email else None


def _reauthorize_required(source: Source) -> bool:
    """Whether the grant is dead (ADR-0019 §1): the sync task's sentinel, or a
    vanished credential (``auth_secret_ref`` SET NULL) on a connected source."""
    if source.last_error == REAUTHORIZE_REQUIRED:
        return True
    return source.auth_secret_ref is None and source.status != SourceStatus.PENDING_AUTH


def _to_response(source: Source) -> SourceResponse | GdriveSourceResponse:
    """Project a source onto its contract branch (discriminated by ``type``)."""
    if source.type == "web":
        return SourceResponse(
            id=source.id,
            type=source.type,
            config=_config_response(source.config),
            status=source.status,
            indexed_count=source.indexed_count,
            last_synced_at=source.last_synced_at,
            last_error=source.last_error,
            owner_id=source.owner_id,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )
    return GdriveSourceResponse(
        id=source.id,
        type=source.type,
        config=_gdrive_config_response(source.config),
        status=source.status,
        indexed_count=source.indexed_count,
        last_synced_at=source.last_synced_at,
        last_error=source.last_error,
        connected_account=_connected_account_response(source),
        acl_synced_at=None,  # populated by the ACL mirror (F-CB-2)
        unmapped_acl_count=None,  # populated by the ACL mirror (F-CB-2)
        reauthorize_required=_reauthorize_required(source),
        owner_id=source.owner_id,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _to_list_response(page: SourcePage) -> SourceListResponse:
    return SourceListResponse(
        items=[_to_response(s) for s in page.items],
        next_cursor=page.next_cursor,
    )


def _build_service(
    *,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    request: Request,
) -> SourcesService:
    """Assemble the per-request service from the identity + adapter + audit seams.

    The object store is the injected #22 adapter (the only object-store caller),
    used by ``delete`` to remove the ingested documents' backing objects (#269).
    """
    return SourcesService(
        session,
        tenant_id=tenant_id,
        owner_id=principal.user_id,
        roles=principal.roles,
        object_store=object_store,
        audit=make_audit_sink(tenant_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


# --- Routes -----------------------------------------------------------------


@router.get("", response_model=SourceListResponse)
async def list_sources(
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SourceListResponse:
    """List the caller's own connected sources (cursor-paginated, newest first)."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    page = await service.list_page(cursor=cursor, limit=limit)
    return _to_list_response(page)


@router.post(
    "",
    response_model=SourceWireResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_source(
    body: SourceCreateRequest,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
) -> SourceResponse | GdriveSourceResponse:
    """Add a source — a web URL (any member) or a managed connector (admin only).

    Web: validates + SSRF-checks the URL (ADR-0009 §3) — a blocked or invalid
    URL is **422** (``url_blocked``); the source returns ``pending`` with the
    first sync enqueued. Managed (``gdrive``): admin-gated at action time
    (ADR-0019 §1 — non-admin is **403**, audited); the source is created
    ``pending_auth`` and syncs only after the connect flow completes. Errors are
    typed ``AppError``\\ s mapped by the global handler.
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    if isinstance(body, WebSourceCreateRequest):
        source = await service.add(source_type=body.type, url=body.url)
    else:
        source = await service.add(
            source_type=body.type,
            config=body.config.model_dump(exclude_none=True),
        )
    await session.commit()
    return _to_response(source)


@router.post("/{source_id}/sync", response_model=SourceWireResponse)
async def sync_source(
    source_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
) -> SourceResponse | GdriveSourceResponse:
    """Re-sync one of the caller's sources (re-fetch + re-index); else 404.

    Returns **202** with status ``syncing`` when a fresh sync was enqueued, or
    **200** (no-op) when the source was already ``syncing`` (the contract's two
    success codes). A non-owner or cross-tenant **web** source is **404**
    (INV-1/INV-2). Managed sources are admin-gated at action time (**403**,
    ADR-0019 §1) and cannot sync while awaiting consent (**409**
    ``source_pending_auth``, INV-8).
    """
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    result = await service.resync(source_id)
    if result is None:
        raise NotFoundError("Source not found.")
    source, enqueued = result
    await session.commit()
    response.status_code = status.HTTP_202_ACCEPTED if enqueued else status.HTTP_200_OK
    return _to_response(source)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: UUID,
    request: Request,
    response: Response,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    make_audit_sink: AuditSinkFactory,
    object_store: ObjectStoreDep,
) -> Response:
    """Delete a source; removes its docs + objects (+ backing collection if empty), else 404."""
    service = _build_service(
        session=session,
        principal=principal,
        tenant_id=tenant_id,
        make_audit_sink=make_audit_sink,
        object_store=object_store,
        request=request,
    )
    deleted = await service.delete(source_id)
    if not deleted:
        raise NotFoundError("Source not found.")
    await session.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


# --- Managed-connector OAuth (ADR-0019 §1; F-CB-1 #452) ---------------------


def _build_oauth_service(
    *,
    session: DbSession,
    settings: SettingsDep,
    state_store: OAuthStateStoreDep,
    token_http: OAuthTokenHttpDep,
    request: Request,
) -> ConnectorOAuthService:
    return ConnectorOAuthService(
        session,
        settings=settings,
        state_store=state_store,
        token_http=token_http,
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


@router.post("/{source_id}/connect", response_model=SourceConnectResponse)
async def connect_source(
    source_id: UUID,
    request: Request,
    session: DbSession,
    principal: CurrentUser,
    tenant_id: CurrentTenant,
    settings: SettingsDep,
    state_store: OAuthStateStoreDep,
    token_http: OAuthTokenHttpDep,
) -> SourceConnectResponse:
    """Start (or restart) a managed source's OAuth consent flow (admin only).

    Admin-gated **at action time** in the service (a denial is audited
    ``permission.denied`` before the 403 — INV-5/INV-6). A non-OAuth type is
    **409** ``oauth_not_supported``; a mid-sync source is **409**
    ``not_connectable``; unknown/foreign is **404** (INV-1). The returned URL
    carries only the opaque single-use state handle (ADR-0019 §1).

    ``tenant_id`` is depended on for its side effect (RLS GUC binding); the
    service reads the tenant from the principal.
    """
    del tenant_id  # dependency retained for the RLS bind side effect
    service = _build_oauth_service(
        session=session,
        settings=settings,
        state_store=state_store,
        token_http=token_http,
        request=request,
    )
    authorization_url = await service.start_connect(source_id, principal=principal)
    await session.commit()
    return SourceConnectResponse(authorization_url=authorization_url)


@router.get("/oauth/callback", include_in_schema=True)
async def oauth_callback(
    request: Request,
    session: DbSession,
    settings: SettingsDep,
    state_store: OAuthStateStoreDep,
    token_http: OAuthTokenHttpDep,
    state: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """The provider redirect target — state-authenticated, **always 302**.

    No bearer token (a browser redirect): authentication is the opaque
    single-use ``state`` handle, consumed atomically; the service re-authorizes
    from current state and completes (or fail-closes) the flow. Every query
    parameter is syntactically optional — a missing/garbled ``state`` reaches
    the fail-closed handler and yields the generic ``expired`` error redirect,
    never a validation error (the contract's always-302 guarantee). The
    Location target is the frozen ``{return_url}?connect=...`` shape; no token,
    code, or verifier material ever appears in it.
    """
    service = _build_oauth_service(
        session=session,
        settings=settings,
        state_store=state_store,
        token_http=token_http,
        request=request,
    )
    redirect_url = await service.complete_callback(state=state, code=code, error=error)
    try:
        await session.commit()
    except Exception:  # noqa: BLE001 — the 302 guarantee outranks a commit fault
        await session.rollback()
        return RedirectResponse(service.failure_redirect_url(), status_code=status.HTTP_302_FOUND)
    return RedirectResponse(redirect_url, status_code=status.HTTP_302_FOUND)
