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

from collections.abc import AsyncIterator, Awaitable, Callable
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import InvalidTokenError, Principal, verify_access_token
from app.connectors.oauth import OAuthStateStore, RedisOAuthStateStore
from app.core.config import Settings, get_settings
from app.db.audit_transactions import DurableAuditTransactions
from app.db.repositories import AuditEventRepository
from app.db.session import get_durable_audit_transactions, get_sessionmaker
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditActor
from app.domain.entities import Role
from app.llm import LLMGateway
from app.realtime.backplane import Backplane, RedisBackplane
from app.services.audit import AuditSink, PermissionDeniedContext, PermissionDeniedRecorder
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


async def get_durable_audit_transactions_dep(
    settings: SettingsDep,
) -> DurableAuditTransactions:
    """Process-owned denial provider, initialized serially on the serving loop.

    Keeping this dependency async avoids FastAPI dispatching the lazy singleton
    constructor to multiple worker threads on concurrent first requests.  There
    is no await between the singleton check and engine construction, so only one
    owned audit pool can be installed and later disposed.
    """
    return get_durable_audit_transactions(settings)


DurableAuditTransactionsDep = Annotated[
    DurableAuditTransactions,
    Depends(get_durable_audit_transactions_dep),
]


class AuditSinkFactoryValue:
    """Request-scoped factory for atomic action sinks and durable denials."""

    def __init__(
        self,
        session: AsyncSession,
        transactions: DurableAuditTransactions,
    ) -> None:
        self._session = session
        self._transactions = transactions

    def __call__(self, tenant_id: UUID) -> AuditSink:
        return AuditSink(AuditEventRepository(self._session, tenant_id))

    def denials(self, tenant_id: UUID) -> PermissionDeniedRecorder:
        return PermissionDeniedRecorder(
            self._transactions,
            tenant_id=tenant_id,
            request_session=self._session,
        )

    def denial_context(
        self,
        tenant_id: UUID,
        *,
        actor: AuditActor,
        request_id: str,
        source_ip: str,
    ) -> PermissionDeniedContext:
        """Bundle the canonical recorder with trusted request attribution."""
        return PermissionDeniedContext(
            self.denials(tenant_id),
            actor=actor,
            request_id=request_id,
            source_ip=source_ip,
        )


def make_audit_sink_factory(
    session: DbSession,
    transactions: DurableAuditTransactionsDep,
) -> AuditSinkFactoryValue:
    """Yield a factory that builds a tenant-scoped audit sink (CC-8, spec 0004 §2.4).

    The product-audit sink (mission filter #4) must be tenant-scoped, but the
    tenant is resolved per-request in ``auth/`` — a reserved seam (CC-3). So the
    injectable is a **factory**: a feature that already holds the resolved
    tenant calls ``make_sink(tenant_id)`` to get a sink bound to that tenant
    over *this* request's session, then ``await sink.emit(...)``. The audit row
    flushes within the request transaction, committing atomically with the
    action it records. When ``current_tenant`` lands, a thin ``audit_sink``
    dependency can close over it and call this directly.
    """

    return AuditSinkFactoryValue(session, transactions)


AuditSinkFactory = Annotated[AuditSinkFactoryValue, Depends(make_audit_sink_factory)]


def authenticated_denial_context(
    make_audit_sink: AuditSinkFactoryValue,
    *,
    tenant_id: UUID,
    principal: Principal,
    request: Request,
) -> PermissionDeniedContext:
    """The one API seam for token-bound durable-denial attribution."""
    return make_audit_sink.denial_context(
        tenant_id,
        actor=AuditActor.user(principal.user_id),
        request_id=extract_request_id(request) or "unknown",
        source_ip=request.client.host if request.client else "unknown",
    )


@lru_cache(maxsize=1)
def get_llm_gateway() -> LLMGateway:
    """Process-wide LLM gateway singleton."""
    return LLMGateway(get_settings())


def get_llm_gateway_dep() -> LLMGateway:
    """LLM-gateway dependency (delegates to the cached singleton).

    A thin injectable wrapper over :func:`get_llm_gateway` so a router obtains the
    gateway adapter by injection (and tests can override it with a fake via
    ``app.dependency_overrides``, keeping LLM-dependent API tests offline-safe). The
    gateway remains the single LiteLLM caller (ADR-0004).
    """
    return get_llm_gateway()


LLMGatewayDep = Annotated[LLMGateway, Depends(get_llm_gateway_dep)]


@lru_cache(maxsize=1)
def get_backplane() -> Backplane:
    """Process-wide realtime pub/sub backplane singleton (CC-6 #24).

    The Redis-backed fan-out the chat answer producer publishes to and the WS
    consumer subscribes from. Returned as the :class:`Backplane` Protocol so the
    offline tests override it with an in-memory implementation (the streaming
    lifecycle is then exercised without a running Redis) — ``realtime/`` stays the
    only Redis owner (ADR-0004).

    The post-terminal grace (#489) is wired from ``Settings`` so the WS relay holds
    a ``done(pendingSuggestions=true)`` subscription open long enough to relay the
    one trailing ``event:suggestions`` (the suggestions timeout plus a margin).
    """
    settings = get_settings()
    return RedisBackplane(
        settings.redis_url,
        post_terminal_grace_seconds=settings.chat_suggestions_grace_seconds,
    )


async def aclose_backplane() -> None:
    """Release the process-wide backplane's pooled Redis client (issue #487).

    Called from the app lifespan's shutdown, alongside ``aclose_search_store`` /
    ``dispose_engine``, so the client is closed on the same (serving) loop that
    created it. Guarded by the concrete type: the offline in-memory backplane
    owns no client, and the singleton is a Protocol to the rest of the app.
    """
    backplane = get_backplane()
    if isinstance(backplane, RedisBackplane):
        await backplane.aclose()


def get_backplane_dep() -> Backplane:
    """Backplane dependency (delegates to the cached singleton)."""
    return get_backplane()


BackplaneDep = Annotated[Backplane, Depends(get_backplane_dep)]


@lru_cache(maxsize=1)
def get_oauth_state_store() -> OAuthStateStore:
    """Process-wide connector-OAuth state store (ADR-0019 §1).

    The Redis-backed single-use flow-record store behind the opaque ``state``
    handles. Returned as the :class:`OAuthStateStore` Protocol so the offline
    tests override it with the in-memory implementation.
    """
    return RedisOAuthStateStore(get_settings().redis_url)


def get_oauth_state_store_dep() -> OAuthStateStore:
    """State-store dependency (delegates to the cached singleton)."""
    return get_oauth_state_store()


OAuthStateStoreDep = Annotated[OAuthStateStore, Depends(get_oauth_state_store_dep)]


async def get_oauth_token_http() -> AsyncIterator[httpx.AsyncClient]:
    """A request-scoped bounded HTTP client for the OAuth token exchange.

    Fresh per request and closed with it (the exchange happens inside the
    interactive callback). Tests override this with a MockTransport client so
    the flow tests run fully offline.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        yield client


OAuthTokenHttpDep = Annotated[httpx.AsyncClient, Depends(get_oauth_token_http)]


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Process-wide object-store adapter singleton."""
    return ObjectStore(get_settings())


def get_object_store_dep() -> ObjectStore:
    """Object-store dependency (delegates to the cached singleton).

    A thin injectable wrapper over :func:`get_object_store` so a router obtains
    the #22 adapter by injection (and tests can override it with an in-memory
    fake via ``app.dependency_overrides``, keeping object-store-dependent API
    tests offline-safe). The adapter remains the single object-store caller.
    """
    return get_object_store()


ObjectStoreDep = Annotated[ObjectStore, Depends(get_object_store_dep)]


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


async def current_tenant(principal: CurrentUser, session: DbSession) -> UUID:
    """The tenant id bound at the token — the scope for ``db/`` repositories.

    A thin projection of ``current_user`` so a router that needs only the tenant
    (e.g. to construct a tenant-scoped repository) does not pass the whole
    principal around. Never sourced from a header or body (INV-1 begins here).

    **It also binds the RLS GUC** (``app.tenant_id``) on the request session for
    this transaction (``app.db.tenant_context.bind_tenant``, #17): because nearly
    every tenant-scoped route depends on ``current_tenant``, resolving the tenant
    here is the single seam that arms the Postgres RLS backstop on the request
    path. A no-op on the offline SQLite engine the tests use. Tenant-scoped routes
    that take a ``DbSession`` directly therefore get the GUC bound automatically;
    the pre-identity auth paths (login/refresh) set the bypass sentinel in the
    service instead, since the tenant is not yet known there.
    """
    await bind_tenant(session, principal.tenant_id)
    return principal.tenant_id


CurrentTenant = Annotated[UUID, Depends(current_tenant)]


def require_roles(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that admits only principals holding one of ``roles``.

    The reusable RBAC seam for role-gated routes (INV-5): a wave-2 admin route
    declares ``Depends(require_roles(Role.ADMIN))``. The actual check delegates
    to ``services.auth_service.require_role`` (role checks live in ``services/``,
    spec 0004 §2.3); a principal lacking every listed role gets a 403, distinct
    from the 401 an unauthenticated caller gets.
    """

    if not roles:
        raise ValueError("require_roles needs at least one role")

    async def _dependency(
        request: Request,
        principal: CurrentUser,
        tenant_id: CurrentTenant,
        make_audit_sink: AuditSinkFactory,
    ) -> Principal:
        if any(principal.has_role(r) for r in roles):
            return principal
        # This dependency runs only after `current_user` + `current_tenant`, so
        # attribution is token-bound and RLS is armed. The matched route template
        # is server-owned (unlike a body or query parameter), making it safe causal
        # context for the denial without disclosing any protected resource.
        route = request.scope.get("route")
        route_path = str(getattr(route, "path", request.url.path))
        denials = make_audit_sink.denials(tenant_id)
        await denials.emit(
            actor=AuditActor.user(principal.user_id),
            resource_type="api_route",
            resource_id=route_path,
            attempted_action=f"{request.method.upper()} {route_path}",
            reason="missing_required_role",
            request_id=extract_request_id(request) or "unknown",
            source_ip=request.client.host if request.client else "unknown",
            required_roles=tuple(role.value for role in roles),
        )
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
    return (
        request.headers.get("x-request-id")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "request_id", None)
    )
