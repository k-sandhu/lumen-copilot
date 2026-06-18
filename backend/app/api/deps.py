"""FastAPI dependencies — the wiring routers pull from.

Provides the request-scoped DB session, the settings accessor, the adapter
singletons (LLM gateway, object store), and the **identity seam** every wave-2
endpoint reuses: ``current_user`` (the resolved :class:`Principal`) and
``current_tenant`` (the tenant id bound at the token). Identity is resolved
**only** through ``app.auth`` (ADR-0004: the one token validator); routers and
services receive the principal by injection and never decode a token or trust a
client-supplied tenant (spec 0004 §2.3).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import InvalidTokenError, Principal, verify_access_token
from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.domain.entities import Role
from app.llm import LLMGateway
from app.services.auth_service import require_role
from app.storage import ObjectStore


def get_settings_dep() -> Settings:
    """Settings dependency (delegates to the cached singleton)."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped async DB session.

    The session is closed when the request ends; it is committed by the caller
    on success. This is the only way a router/service obtains a session — the
    session factory itself stays inside ``app.db``.
    """
    factory = get_sessionmaker()
    async with factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


@lru_cache(maxsize=1)
def get_llm_gateway() -> LLMGateway:
    """Process-wide LLM gateway singleton."""
    return LLMGateway(get_settings())


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Process-wide object-store adapter singleton."""
    return ObjectStore(get_settings())


# --- Identity seam (CC-3 / spec 0004 §2.3) ---------------------------------
#
# ``auto_error=False`` so a missing Authorization header reaches our handler and
# becomes a typed 401 (problem+json, INV-4) rather than FastAPI's default body.
_bearer_scheme = HTTPBearer(auto_error=False)
BearerCreds = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)]


async def current_user(creds: BearerCreds, settings: SettingsDep) -> Principal:
    """Resolve the authenticated principal from the bearer token.

    **The** identity dependency — every protected route depends on this (or a
    role-gated wrapper of it). The token is validated only in ``app.auth``
    (ADR-0004); the tenant comes from the token's claim, never from request
    input (spec 0004 §2.3). A missing, malformed, expired, or otherwise invalid
    token raises :class:`InvalidTokenError` → 401 (INV-4).
    """
    if creds is None or not creds.credentials:
        raise InvalidTokenError()
    return verify_access_token(creds.credentials, settings)


CurrentUser = Annotated[Principal, Depends(current_user)]


async def current_tenant(principal: CurrentUser) -> UUID:
    """The tenant id bound at the token — the scope for ``db/`` repositories.

    A thin projection of ``current_user`` so a router that needs only the tenant
    (e.g. to construct a tenant-scoped repository) does not pass the whole
    principal around. Never sourced from a header or body (INV-1 begins here).
    """
    return principal.tenant_id


CurrentTenant = Annotated[UUID, Depends(current_tenant)]


def require_roles(*roles: Role) -> Callable[[Principal], Principal]:
    """Build a dependency that admits only principals holding one of ``roles``.

    The reusable RBAC seam for role-gated routes (INV-5): a wave-2 admin route
    declares ``Depends(require_roles(Role.ADMIN))``. The actual check delegates
    to ``services.auth_service.require_role`` (role checks live in ``services/``,
    spec 0004 §2.3); a principal lacking every listed role gets a 403, distinct
    from the 401 an unauthenticated caller gets.
    """

    def _dependency(principal: CurrentUser) -> Principal:
        if any(principal.has_role(r) for r in roles):
            return principal
        # Reuse the single-role assertion to raise the typed 403; pass the first
        # required role for the message (the set is small and homogeneous here).
        require_role(principal, roles[0])
        return principal  # pragma: no cover — require_role raised above

    return _dependency


def extract_request_id(request: Request) -> str | None:
    """The inbound/minted request id (set by the correlation middleware echo).

    Used by routers to thread the correlation id into audit events without the
    service reaching into the request object (boundary discipline).
    """
    return request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
