"""Auth routes — login, refresh, logout, me (CC-3 / spec 0004 §2.3).

Contract-first: shapes match ``contracts/openapi.yaml`` (``LoginRequest`` →
``TokenResponse``; ``CurrentUser``). Routers validate in → call **one** service
→ shape out (ADR-0004): all orchestration is in ``services.auth_service``; this
layer only (de)serializes, scopes the refresh cookie, and threads
correlation context.

The refresh token rides an **httpOnly, SameSite=strict** cookie (never the JSON
body) so client JS cannot read it; the short-lived access token is returned in
the body for the client to send as a bearer header. ``/auth/login`` and
``/auth/refresh`` are unauthenticated (contract ``security: []``); ``/auth/me``
and ``/auth/logout`` require the bearer token.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, Field

from app.api.deps import (
    CurrentTenant,
    CurrentUser,
    DbSession,
    ObjectStoreDep,
    SettingsDep,
    extract_request_id,
)
from app.auth import InvalidTokenError
from app.core.config import Settings
from app.db.repositories import TenantRepository, UserRepository
from app.services.auth_service import AuthService, AuthSlotCollisionError, IssuedTokens

router = APIRouter(prefix="/auth", tags=["auth"])

# The httpOnly refresh-token cookie. Scoped to the refresh/logout paths so it is
# not sent on every request — only where it is consumed.
_REFRESH_COOKIE = "lumen_refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"
_AUTH_SLOT_HEADER = "X-Lumen-Auth-Slot"
_PREVIOUS_AUTH_SLOT_HEADER = "X-Lumen-Previous-Auth-Slot"
_AUTH_SLOT_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_AUTH_SLOT_COOKIE_PREFIX = f"{_REFRESH_COOKIE}_"
_AUTH_SLOT_RE = re.compile(_AUTH_SLOT_PATTERN)
# One deletion header is roughly 150 bytes with the required cookie attributes.
# Eight plus the newly issued cookie stays comfortably below a common 8 KiB
# aggregate response-header limit, even after ordinary non-cookie headers.
_MAX_STALE_COOKIE_DELETIONS = 8


# --- Wire models (mirror contracts/openapi.yaml) ---------------------------


class LoginRequest(BaseModel):
    """``#/components/schemas/LoginRequest`` — email + password.

    ``email`` is a plain string (the contract's ``format: email`` is an OpenAPI
    hint): credential checking happens against the store, and a non-existent or
    malformed address yields the same generic 401, so strict address parsing
    here would only add a dependency without changing behavior.
    """

    model_config = {"extra": "forbid"}

    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    """``#/components/schemas/TokenResponse`` — the access token + lifetime."""

    model_config = {"extra": "forbid"}

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class CurrentUserResponse(BaseModel):
    """``#/components/schemas/CurrentUser`` — id, email, tenant, roles, logo."""

    model_config = {"extra": "forbid"}

    id: str
    email: str
    tenant_id: str
    tenant_name: str
    roles: list[str]
    created_at: datetime
    # The tenant's application logo as a short-TTL presigned GET URL, or null when
    # none is set (the shell then renders the default brand mark). Per-tenant
    # branding — the same for every user of the tenant (admin uploads it).
    logo_url: str | None = None
    # The caller's own profile avatar as a short-TTL presigned GET URL, or null when
    # none is set (the shell then renders the initials fallback). Per-user — the user
    # uploads it for their own account (PUT /me/avatar).
    avatar_url: str | None = None


# --- Helpers ----------------------------------------------------------------


def _refresh_cookie_name(auth_slot: UUID | None) -> str:
    """One cookie name per browser auth intent; slot identity grants no access."""
    return _REFRESH_COOKIE if auth_slot is None else f"{_REFRESH_COOKIE}_{auth_slot}"


def _presented_refresh_token(request: Request, auth_slot: UUID | None) -> str | None:
    return request.cookies.get(_refresh_cookie_name(auth_slot))


def _auth_slot_uuid(raw: str | None) -> UUID | None:
    """Convert only after FastAPI validated the canonical raw wire spelling."""
    return UUID(raw) if raw is not None else None


def _presented_auth_slot_cookies(request: Request) -> frozenset[UUID]:
    """Return strict slot ids from cookie *names* as untrusted cleanup hints.

    HttpOnly prevents JavaScript enumeration, but the server can see names on
    the request.  A hint grants no authority: admission later intersects these
    ids with stale rows belonging to the verified tenant/user before emitting a
    bounded deletion batch.
    """
    slots: set[UUID] = set()
    for name in request.cookies:
        if not name.startswith(_AUTH_SLOT_COOKIE_PREFIX):
            continue
        raw = name[len(_AUTH_SLOT_COOKIE_PREFIX) :]
        if _AUTH_SLOT_RE.fullmatch(raw) is not None:
            slots.add(UUID(raw))
    return frozenset(slots)


def _delete_refresh_cookie(
    response: Response,
    settings: Settings,
    auth_slot: UUID,
) -> None:
    response.delete_cookie(
        key=_refresh_cookie_name(auth_slot),
        httponly=True,
        secure=settings.environment != "local",
        samesite="strict",
        path=_REFRESH_COOKIE_PATH,
    )


def _set_refresh_cookie(
    response: Response,
    tokens: IssuedTokens,
    settings: Settings,
    auth_slot: UUID | None,
) -> None:
    """Attach the rotating refresh token as an httpOnly cookie.

    ``secure`` is on outside ``local`` (HTTPS-only there); kept off locally so
    plain-HTTP dev still works. ``SameSite=strict`` blocks cross-site sends.
    """
    response.set_cookie(
        key=_refresh_cookie_name(auth_slot),
        value=tokens.refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.environment != "local",
        samesite="strict",
        path=_REFRESH_COOKIE_PATH,
    )
    # Admission returns only tenant/user-owned stale slot ids and never the new
    # or selected previous slot. Expire those exact HttpOnly names in the same
    # response that establishes the new cookie; JS need not enumerate them.
    for stale_slot in tokens.cleanup_auth_slots:
        if stale_slot != auth_slot:
            _delete_refresh_cookie(response, settings, stale_slot)


def _token_response(tokens: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=tokens.access.token,
        expires_in=tokens.access.expires_in,
    )


# --- Routes -----------------------------------------------------------------


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    auth_slot: Annotated[
        str,
        Header(
            alias=_AUTH_SLOT_HEADER,
            pattern=_AUTH_SLOT_PATTERN,
            json_schema_extra={"format": "uuid"},
        ),
    ] = None,  # type: ignore[assignment]
    previous_auth_slot: Annotated[
        str,
        Header(
            alias=_PREVIOUS_AUTH_SLOT_HEADER,
            pattern=_AUTH_SLOT_PATTERN,
            json_schema_extra={"format": "uuid"},
        ),
    ] = None,  # type: ignore[assignment]
) -> TokenResponse:
    """Exchange email + password for an access token (sets refresh cookie).

    Invalid credentials → a generic 401 with no account-existence disclosure
    (the service raises ``InvalidCredentialsError``; spec 0004 §2.3).
    """
    service = AuthService(session, settings)
    slot_id = _auth_slot_uuid(auth_slot)
    try:
        tokens = await service.login(
            email=body.email,
            password=body.password,
            request_id=extract_request_id(request),
            source_ip=request.client.host if request.client else "unknown",
            session_id=slot_id,
            previous_session_id=_auth_slot_uuid(previous_auth_slot),
            presented_cookie_session_ids=_presented_auth_slot_cookies(request),
            cleanup_limit=_MAX_STALE_COOKIE_DELETIONS,
        )
    except AuthSlotCollisionError:
        # The conflicting insert rolled back only its nested savepoint; commit
        # the outer transaction containing the mandatory denied audit, then let
        # the canonical error handler render 409. Audit/commit failure is 500.
        await session.commit()
        raise
    await session.commit()
    _set_refresh_cookie(response, tokens, settings, slot_id)
    return _token_response(tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    auth_slot: Annotated[
        str,
        Header(
            alias=_AUTH_SLOT_HEADER,
            pattern=_AUTH_SLOT_PATTERN,
            json_schema_extra={"format": "uuid"},
        ),
    ] = None,  # type: ignore[assignment]
) -> TokenResponse:
    """Rotate the refresh cookie into a fresh access token + refresh cookie.

    A missing/expired/revoked/replayed token → 401 (INV-4).
    """
    slot_id = _auth_slot_uuid(auth_slot)
    service = AuthService(session, settings)
    tokens = await service.refresh(
        raw_refresh_token=_presented_refresh_token(request, slot_id),
        request_id=extract_request_id(request),
        source_ip=request.client.host if request.client else "unknown",
        session_id=slot_id,
    )
    await session.commit()
    _set_refresh_cookie(response, tokens, settings, slot_id)
    return _token_response(tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    principal: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    auth_slot: Annotated[
        str,
        Header(
            alias=_AUTH_SLOT_HEADER,
            pattern=_AUTH_SLOT_PATTERN,
            json_schema_extra={"format": "uuid"},
        ),
    ] = None,  # type: ignore[assignment]
) -> Response:
    """Revoke the selected refresh-token family (requires a bearer token).

    Slot-aware responses expire only their unique cookie name, so a late logout
    cannot erase a newer login's distinct cookie. Legacy shared-cookie responses
    intentionally omit deletion; server-side revocation still ends the session.
    """
    slot_id = _auth_slot_uuid(auth_slot)
    service = AuthService(session, settings)
    await service.logout(
        principal,
        raw_refresh_token=_presented_refresh_token(request, slot_id),
        request_id=extract_request_id(request),
        source_ip=request.client.host if request.client else "unknown",
        session_id=slot_id,
    )
    await session.commit()
    if slot_id is not None:
        _delete_refresh_cookie(response, settings, slot_id)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user(
    principal: CurrentUser,
    session: DbSession,
    tenant_id: CurrentTenant,
    object_store: ObjectStoreDep,
) -> CurrentUserResponse:
    """The authenticated principal (id, tenant, roles, logo) — from the token, hydrated.

    The token carries id/tenant/roles; we read the user row (tenant-scoped) for
    the email + ``created_at`` and the tenant row for ``tenant_name`` (so the UI
    never has to surface the raw tenant UUID, #247) and the per-tenant application
    ``logo_key``. When a logo is set, we mint a short-TTL presigned GET URL for the
    shell to render (else ``logo_url`` is null → the default brand mark). The user's
    own ``avatar_key`` is presigned the same way into ``avatar_url`` (null → the shell
    renders the initials fallback). A token whose subject no longer exists is treated
    as unauthenticated (401).

    Depends on ``CurrentTenant`` (not just the principal's ``tenant_id``) so the
    RLS GUC is bound on this request session before the tenant-scoped read (#17);
    the resolved tenant equals ``principal.tenant_id``.
    """
    user = await UserRepository(session, tenant_id).get(principal.user_id)
    if user is None:
        raise InvalidTokenError()
    tenant = await TenantRepository(session).get(tenant_id)
    if tenant is None:  # pragma: no cover — a resolved principal always has a tenant
        raise InvalidTokenError()
    logo_url = (
        await object_store.presign_get(str(tenant_id), tenant.logo_key)
        if tenant.logo_key is not None
        else None
    )
    # The caller's own profile avatar, presigned the same way (per-user, not
    # per-tenant): null when unset → the shell renders the initials fallback.
    avatar_url = (
        await object_store.presign_get(str(tenant_id), user.avatar_key)
        if user.avatar_key is not None
        else None
    )
    return CurrentUserResponse(
        id=str(user.id),
        email=user.email,
        tenant_id=str(user.tenant_id),
        tenant_name=tenant.name,
        roles=[r.value for r in user.roles],
        created_at=user.created_at,
        logo_url=logo_url,
        avatar_url=avatar_url,
    )
