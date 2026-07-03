"""Auth routes — login, refresh, logout, me (CC-3 / spec 0004 §2.3).

Contract-first: shapes match ``contracts/openapi.yaml`` (``LoginRequest`` →
``TokenResponse``; ``CurrentUser``). Routers validate in → call **one** service
→ shape out (ADR-0004): all orchestration is in ``services.auth_service``; this
layer only (de)serializes, sets/clears the refresh cookie, and threads
correlation context.

The refresh token rides an **httpOnly, SameSite=strict** cookie (never the JSON
body) so client JS cannot read it; the short-lived access token is returned in
the body for the client to send as a bearer header. ``/auth/login`` and
``/auth/refresh`` are unauthenticated (contract ``security: []``); ``/auth/me``
and ``/auth/logout`` require the bearer token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Request, Response, status
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
from app.services.auth_service import AuthService, IssuedTokens

router = APIRouter(prefix="/auth", tags=["auth"])

# The httpOnly refresh-token cookie. Scoped to the refresh/logout paths so it is
# not sent on every request — only where it is consumed.
_REFRESH_COOKIE = "lumen_refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"


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


# --- Helpers ----------------------------------------------------------------


def _set_refresh_cookie(response: Response, tokens: IssuedTokens, settings: Settings) -> None:
    """Attach the rotating refresh token as an httpOnly cookie.

    ``secure`` is on outside ``local`` (HTTPS-only there); kept off locally so
    plain-HTTP dev still works. ``SameSite=strict`` blocks cross-site sends.
    """
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=tokens.refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.environment != "local",
        samesite="strict",
        path=_REFRESH_COOKIE_PATH,
    )


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
) -> TokenResponse:
    """Exchange email + password for an access token (sets refresh cookie).

    Invalid credentials → a generic 401 with no account-existence disclosure
    (the service raises ``InvalidCredentialsError``; spec 0004 §2.3).
    """
    service = AuthService(session, settings)
    tokens = await service.login(
        email=body.email,
        password=body.password,
        request_id=extract_request_id(request),
        source_ip=request.client.host if request.client else None,
    )
    await session.commit()
    _set_refresh_cookie(response, tokens, settings)
    return _token_response(tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: DbSession,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie(alias=_REFRESH_COOKIE)] = None,
) -> TokenResponse:
    """Rotate the refresh cookie into a fresh access token + refresh cookie.

    A missing/expired/revoked/replayed token → 401 (INV-4).
    """
    service = AuthService(session, settings)
    tokens = await service.refresh(
        raw_refresh_token=refresh_token,
        request_id=extract_request_id(request),
        source_ip=request.client.host if request.client else None,
    )
    await session.commit()
    _set_refresh_cookie(response, tokens, settings)
    return _token_response(tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    principal: CurrentUser,
    session: DbSession,
    settings: SettingsDep,
    refresh_token: Annotated[str | None, Cookie(alias=_REFRESH_COOKIE)] = None,
) -> Response:
    """Revoke the refresh token and clear the cookie (requires a bearer token)."""
    service = AuthService(session, settings)
    await service.logout(
        principal,
        raw_refresh_token=refresh_token,
        request_id=extract_request_id(request),
        source_ip=request.client.host if request.client else None,
    )
    await session.commit()
    response.delete_cookie(key=_REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH)
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
    shell to render (else ``logo_url`` is null → the default brand mark). A token
    whose subject no longer exists is treated as unauthenticated (401).

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
    return CurrentUserResponse(
        id=str(user.id),
        email=user.email,
        tenant_id=str(user.tenant_id),
        tenant_name=tenant.name,
        roles=[r.value for r in user.roles],
        created_at=user.created_at,
        logo_url=logo_url,
    )
