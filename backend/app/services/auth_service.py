"""Auth use-cases — login, refresh, logout (spec 0004 §2.3).

The orchestration layer (ADR-0004: ``services/`` compose adapters; routers call
exactly one service). It pairs ``auth/`` (hashing, token mint/verify — the only
token validator) with the ``db/`` repositories (the only SQL) and emits the
product-audit events for auth (``auth.login`` / ``auth.login_failed`` /
``auth.logout``, spec 0004 §2.4 — through the one audit sink).

Tenant binding (spec 0004 §2.3): a principal belongs to exactly one tenant,
bound **at the token**. Because repositories are tenant-scoped, login must first
resolve which tenant an email belongs to. For the MVP (one principal → one
tenant, email unique within a tenant) we resolve the user across tenants via a
tenant-agnostic lookup, then scope every subsequent operation to that tenant.

Failures are uniform: a wrong password and an unknown account both raise
:class:`InvalidCredentialsError` → a generic 401 (no account-existence
disclosure). An invalid/expired/revoked refresh token raises
:class:`InvalidTokenError` → 401 (INV-4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    InvalidCredentialsError,
    InvalidTokenError,
    MintedAccessToken,
    Principal,
    dummy_verify_async,
    generate_refresh_token,
    hash_refresh_token,
    mint_access_token,
    verify_password_async,
)
from app.core.config import Settings
from app.core.errors import ForbiddenError
from app.db.repositories import (
    AuditEventRepository,
    RefreshTokenRepository,
    UserLookupRepository,
    UserRepository,
)
from app.db.tenant_context import bind_bypass, bind_tenant
from app.domain.entities import AuditOutcome, Role, User


def _as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC; pass an aware one through.

    The ``expires_at`` column is ``timezone=True`` so Postgres returns an aware
    value, but SQLite (used by the offline tests) has no tz type and returns a
    naive one. Normalizing here keeps the expiry comparison correct on both —
    the stored instants are always UTC by construction (``datetime.now(UTC)``).
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    """The result of a successful login/refresh.

    ``access`` is the bearer JWT (+ its lifetime for ``expires_in``);
    ``refresh_token`` is the opaque rotating token the router sets as an
    httpOnly cookie (it is never returned in the JSON body).
    """

    access: MintedAccessToken
    refresh_token: str


class AuthService:
    """Login / refresh / logout, plus RBAC role assertion (spec 0004 §2.3)."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # --- internal helpers ---------------------------------------------------

    async def _issue_tokens(self, user: User) -> IssuedTokens:
        principal = Principal.from_user(user)
        access = mint_access_token(principal, self._settings)
        raw_refresh = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=self._settings.refresh_token_ttl_seconds)
        await RefreshTokenRepository(self._session, user.tenant_id).create(
            user_id=user.id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=expires_at,
        )
        return IssuedTokens(access=access, refresh_token=raw_refresh)

    # --- use-cases ----------------------------------------------------------

    async def login(
        self,
        *,
        email: str,
        password: str,
        request_id: str | None = None,
        source_ip: str | None = None,
    ) -> IssuedTokens:
        """Verify credentials and issue an access + refresh token pair.

        Raises:
            InvalidCredentialsError: no such user OR wrong password — identical
                401 either way (no account-existence disclosure, spec 0004 §2.3).
        """
        # Pre-identity: the tenant is not yet known, so the email lookup is
        # tenant-agnostic by design. Opt this transaction into the RLS bypass
        # sentinel (#17) — a no-op off Postgres — so the lookup and the
        # subsequent tenant-scoped writes (refresh token + audit) are permitted
        # under row-level security. Everything written still carries the
        # resolved user's tenant_id (the repository sets it explicitly).
        await bind_bypass(self._session)
        user = await UserLookupRepository(self._session).find_by_email(email)
        if user is None:
            # Burn a verify's worth of CPU so timing does not reveal non-existence.
            # Off the event loop (#512): this path needs no account, so leaving it
            # inline lets any caller park the worker with invented addresses.
            await dummy_verify_async()
            await self._record_login_failed(
                email=email, request_id=request_id, source_ip=source_ip, tenant_id=None
            )
            raise InvalidCredentialsError()

        # Argon2id is CPU-hard by design — verified on a thread so a login does
        # not stall every other request in this worker (#512).
        if not await verify_password_async(user.password_hash, password):
            # Still under the bypass GUC: the failed-login audit row is written
            # for the resolved user's tenant; re-scope so it lands under exactly
            # that tenant (defense in depth) before the audit write.
            await bind_tenant(self._session, user.tenant_id)
            await self._record_login_failed(
                email=email,
                request_id=request_id,
                source_ip=source_ip,
                tenant_id=user.tenant_id,
                actor_id=user.id,
            )
            raise InvalidCredentialsError()

        # Identity resolved → re-scope the GUC from the bypass sentinel to the
        # exact tenant, so the token + audit writes below are under that tenant's
        # RLS scope, not the broad bypass (#17, defense in depth).
        await bind_tenant(self._session, user.tenant_id)
        tokens = await self._issue_tokens(user)
        await AuditEventRepository(self._session, user.tenant_id).record(
            action="auth.login",
            resource_type="session",
            resource_id=str(user.id),
            outcome=AuditOutcome.ALLOWED,
            actor_id=user.id,
            request_id=request_id,
            source_ip=source_ip,
        )
        return tokens

    async def refresh(
        self,
        *,
        raw_refresh_token: str | None,
        request_id: str | None = None,
        source_ip: str | None = None,
    ) -> IssuedTokens:
        """Rotate a valid refresh token into a fresh access + refresh pair.

        Rotation: the presented token is revoked and a new one issued, so a
        replayed (already-rotated) token fails. An expired, revoked, unknown, or
        missing token raises :class:`InvalidTokenError` → 401 (INV-4).
        """
        if not raw_refresh_token:
            raise InvalidTokenError()

        token_hash = hash_refresh_token(raw_refresh_token)
        # Pre-identity: the refresh cookie carries no tenant, so resolving the
        # owning token row is tenant-agnostic. Opt into the RLS bypass sentinel
        # (#17, a no-op off Postgres) for this lookup; the rotation + re-issue
        # below stay within the resolved token's tenant.
        await bind_bypass(self._session)
        token = await UserLookupRepository(self._session).find_refresh_token_owner(token_hash)
        if token is None or token.revoked_at is not None:
            raise InvalidTokenError()
        if _as_utc(token.expires_at) <= datetime.now(UTC):
            raise InvalidTokenError()

        # Identity resolved → re-scope the GUC from the bypass sentinel to the
        # token's tenant, so the lookups + rotation writes below run under that
        # tenant's RLS scope (#17, defense in depth).
        await bind_tenant(self._session, token.tenant_id)
        user = await UserRepository(self._session, token.tenant_id).get(token.user_id)
        if user is None:
            raise InvalidTokenError()

        # Rotate: revoke the presented token, then issue a new pair.
        await RefreshTokenRepository(self._session, token.tenant_id).revoke(token_hash)
        return await self._issue_tokens(user)

    async def logout(
        self,
        principal: Principal,
        *,
        raw_refresh_token: str | None,
        request_id: str | None = None,
        source_ip: str | None = None,
    ) -> None:
        """Revoke the presented refresh token and audit the logout.

        Idempotent: an absent/already-revoked token is not an error (the client
        is logging out regardless). Revocation is tenant-scoped to the caller.
        """
        # The principal carries the tenant, so bind the RLS GUC to it for the
        # revoke + audit write (#17; a no-op off Postgres). ``/auth/logout`` does
        # not depend on ``current_tenant`` (it takes the principal directly), so
        # the GUC is bound here rather than by the request dependency.
        await bind_tenant(self._session, principal.tenant_id)
        if raw_refresh_token:
            await RefreshTokenRepository(self._session, principal.tenant_id).revoke(
                hash_refresh_token(raw_refresh_token)
            )
        await AuditEventRepository(self._session, principal.tenant_id).record(
            action="auth.logout",
            resource_type="session",
            resource_id=str(principal.user_id),
            outcome=AuditOutcome.ALLOWED,
            actor_id=principal.user_id,
            request_id=request_id,
            source_ip=source_ip,
        )

    async def _record_login_failed(
        self,
        *,
        email: str,
        tenant_id: UUID | None,
        actor_id: UUID | None = None,
        request_id: str | None = None,
        source_ip: str | None = None,
    ) -> None:
        """Audit a failed login (spec 0004 §2.4 ``auth.login_failed``).

        An unknown account has no tenant to scope the event to; we skip the
        write in that case (there is no tenant boundary to attribute it to) —
        the generic 401 still fires. A wrong password for a *known* account is
        attributed to that user's tenant.
        """
        if tenant_id is None:
            return
        await AuditEventRepository(self._session, tenant_id).record(
            action="auth.login_failed",
            resource_type="session",
            outcome=AuditOutcome.DENIED,
            actor_id=actor_id,
            request_id=request_id,
            source_ip=source_ip,
            metadata={"email": email},
        )


def require_role(principal: Principal, role: Role) -> None:
    """Assert the principal holds ``role`` — the RBAC chokepoint (INV-5).

    Role checks live in ``services/`` (spec 0004 §2.3). A principal lacking the
    role gets a 403 (authenticated but not permitted), distinct from the 401 an
    unauthenticated caller gets. This is the seam wave-2 role-gated routes call.

    Raises:
        ForbiddenError: the principal does not hold ``role`` (INV-5 → 403).
    """
    if not principal.has_role(role):
        raise ForbiddenError(f"This action requires the {role.value!r} role.")
