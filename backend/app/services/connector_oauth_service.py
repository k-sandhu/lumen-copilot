"""Managed-connector OAuth use-cases — connect + callback (ADR-0019 §1, #452).

The orchestration for the two ends of the authorization-code + PKCE flow:

* :meth:`ConnectorOAuthService.start_connect` — the JWT-authenticated,
  **admin-gated** initiation: advances the source's connect generation
  (invalidating older in-flight flows), mints the PKCE pair, stores the flow
  bindings server-side under an opaque single-use ``state`` handle, and returns
  the provider consent URL. A denied initiation is **audited**
  (``permission.denied`` from the token-bound actor) and raised as the typed
  403 (INV-5/INV-6).
* :meth:`ConnectorOAuthService.complete_callback` — the state-authenticated
  browser redirect. It **never raises**: every path returns the frozen redirect
  target (``{return_url}?connect=ok&source=...`` /
  ``?connect=error&reason=...``). It consumes the state atomically, then
  **re-authorizes from current state, trusting nothing carried by the flow**:
  the acting user must still hold ``admin`` *in the database* (a demoted
  admin's callback is denied — the JWT that started the flow is not consulted),
  the source must exist, be OAuth-capable, and carry the **same connect
  generation** as the record. Only then is the code exchanged (server-side,
  verifier attached); the refresh token goes straight into the CC-C vault and
  the source row gets ``auth_secret_ref`` — token material never touches the
  row, the logs, the audit trail, or the redirect.

Failure-audit staging (ADR-0019 §1 / spec 0004 §2.4): callback failures that
resolve to a trusted tenant emit ``source.connected`` with
``outcome=denied|error``; an unresolvable ``state`` is ops-telemetry only
(never a tenant-attributed audit row fabricated from attacker input).
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.connectors.base import Connector, get_oauth_spec
from app.connectors.oauth import (
    OAuthExchangeError,
    OAuthFlowRecord,
    OAuthSpec,
    OAuthStateStore,
    TokenResponse,
    build_authorization_url,
    exchange_code,
    generate_pkce,
)
from app.connectors.registry import UnknownConnectorError, get_connector
from app.core.config import Settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.logging import get_logger
from app.db.repositories import AuditEventRepository, SourceRepository, UserRepository
from app.db.tenant_context import bind_tenant
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import AuditOutcome, Role, SecretKind, SourceStatus
from app.services.audit import AuditSink
from app.services.secrets_service import build_secrets_service
from app.services.sources_service import _dispatch_off_loop

log = get_logger(__name__)

# The callback path under the API base — the one redirect target every managed
# connector shares (contract: /sources/oauth/callback).
CALLBACK_PATH = "/api/v1/sources/oauth/callback"

# Frozen redirect reason codes (the contract's closed set).
REASON_EXPIRED = "expired"
REASON_DENIED = "denied"
REASON_PROVIDER_ERROR = "provider_error"
REASON_FAILED = "failed"

# The per-source vault handle: stable so a reconnect rotates the credential in
# place (the vault upsert keeps the same secret row/name per owner).
_SECRET_NAME_PREFIX = "connector_oauth:"


class AccountEmailProbe(Protocol):
    """A connector's optional identity probe (ADR-0019 §1 — Drive: ``about.get``)."""

    async def __call__(self, http: httpx.AsyncClient, access_token: str) -> str | None: ...


class ConnectorOAuthService:
    """Start/complete managed-connector OAuth flows for one request.

    ``start_connect`` runs with a resolved :class:`Principal`; the callback has
    none (state-authenticated) and resolves everything from the consumed flow
    record. ``token_http`` is the injectable client the code exchange (and the
    optional account-email probe) runs over — tests hand in a MockTransport
    client; production passes a plain bounded client.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        state_store: OAuthStateStore,
        token_http: httpx.AsyncClient,
        request_id: str,
        source_ip: str,
    ) -> None:
        self._session = session
        self._settings = settings
        self._states = state_store
        self._http = token_http
        self._request_id = request_id
        self._source_ip = source_ip

    # --- helpers ------------------------------------------------------------

    def _audit(self, tenant_id: UUID) -> AuditSink:
        return AuditSink(AuditEventRepository(self._session, tenant_id))

    def _redirect_uri(self) -> str:
        base = self._settings.connector_oauth_redirect_base_url.rstrip("/")
        return f"{base}{CALLBACK_PATH}"

    def _success_url(self, source_id: UUID) -> str:
        base = self._settings.connector_oauth_frontend_return_url
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{urlencode({'connect': 'ok', 'source': str(source_id)})}"

    def _error_url(self, reason: str) -> str:
        base = self._settings.connector_oauth_frontend_return_url
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{urlencode({'connect': 'error', 'reason': reason})}"

    def failure_redirect_url(self) -> str:
        """The generic ``failed`` redirect — the route's last-resort 302 target."""
        return self._error_url(REASON_FAILED)

    @staticmethod
    def _oauth_connector(source_type: str) -> tuple[Connector, OAuthSpec]:
        """Resolve the connector + its OAuth capability, or raise the typed 409."""
        try:
            connector = get_connector(source_type)
        except UnknownConnectorError as exc:
            raise ConflictError(
                f"source type {source_type!r} has no OAuth flow",
                code="oauth_not_supported",
            ) from exc
        spec = get_oauth_spec(connector)
        if spec is None:
            raise ConflictError(
                f"source type {source_type!r} has no OAuth flow",
                code="oauth_not_supported",
            )
        return connector, spec

    async def _emit_denied_initiation(
        self, tenant_id: UUID, actor_id: UUID, source_id: UUID, reason: str
    ) -> None:
        await self._audit(tenant_id).emit(
            action=AuditAction.PERMISSION_DENIED,
            actor=AuditActor.user(actor_id),
            resource_type="source",
            resource_id=str(source_id),
            outcome=AuditOutcome.DENIED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"reason": reason},
        )

    # --- use-cases ----------------------------------------------------------

    async def start_connect(self, source_id: UUID, *, principal: Principal) -> str:
        """Begin (or restart) the flow; return the provider consent URL.

        Admin-gated **at action time** (ADR-0019 §1): the check runs against the
        principal resolved for THIS request, and a denial is audited before the
        typed 403 (INV-5/INV-6). A source in another tenant (or absent) is 404
        (INV-1); a non-OAuth type is 409 ``oauth_not_supported``; a source mid-
        sync is 409 ``not_connectable``.
        """
        sources = SourceRepository(self._session, principal.tenant_id)
        source = await sources.get(source_id)
        if source is None:
            raise NotFoundError("Source not found.")
        if not principal.has_role(Role.ADMIN):
            await self._emit_denied_initiation(
                principal.tenant_id, principal.user_id, source_id, "not_admin"
            )
            # Commit the deny-audit before the typed 403 aborts the request
            # (INV-6 — nothing else has been written at this point).
            await self._session.commit()
            raise ForbiddenError("Managed-source mutations require the admin role.")
        _connector, spec = self._oauth_connector(source.type)
        if source.status == SourceStatus.SYNCING:
            raise ConflictError(
                "source is mid-sync and cannot start a connect flow",
                code="not_connectable",
            )

        updated = await sources.begin_connect(source_id)
        assert updated is not None  # visibility established above  # noqa: S101

        verifier, challenge = generate_pkce()
        record = OAuthFlowRecord(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            source_id=source_id,
            source_type=source.type,
            code_verifier=verifier,
            redirect_uri=self._redirect_uri(),
            generation=updated.connect_generation,
        )
        state = await self._states.issue(
            record, ttl_seconds=self._settings.connector_oauth_state_ttl_seconds
        )
        return build_authorization_url(
            spec,
            redirect_uri=record.redirect_uri,
            state=state,
            code_challenge=challenge,
        )

    async def complete_callback(
        self, *, state: str | None, code: str | None, error: str | None
    ) -> str:
        """Complete the flow; ALWAYS return a redirect URL (never raise).

        The full fail-closed ladder of ADR-0019 §1 — each rung maps to one of
        the contract's frozen ``reason`` codes; see the module docstring for the
        audit staging.
        """
        try:
            return await self._complete(state=state, code=code, error=error)
        except Exception:  # noqa: BLE001 — the callback must never raise
            log.exception("connector_oauth.callback_failed")
            return self._error_url(REASON_FAILED)

    async def _complete(self, *, state: str | None, code: str | None, error: str | None) -> str:
        # 1. State: consume atomically; anything unresolvable is `expired` —
        #    no tenant attribution from attacker-controlled input, ops-log only.
        if not state:
            log.warning("connector_oauth.callback_no_state")
            return self._error_url(REASON_EXPIRED)
        record = await self._states.consume(state)
        if record is None:
            log.warning("connector_oauth.callback_state_unresolved")
            return self._error_url(REASON_EXPIRED)

        # A trusted tenant is now resolved: bind the RLS GUC for this session
        # (the request carried no token, so current_tenant never ran).
        await bind_tenant(self._session, record.tenant_id)
        audit = self._audit(record.tenant_id)

        async def _fail(reason: str, *, outcome: AuditOutcome, detail: str) -> str:
            await audit.emit(
                action=AuditAction.SOURCE_CONNECTED,
                actor=AuditActor.user(record.user_id),
                resource_type="source",
                resource_id=str(record.source_id),
                outcome=outcome,
                request_id=self._request_id,
                source_ip=self._source_ip,
                metadata={"reason": detail},
            )
            return self._error_url(reason)

        # 2. Re-authorize from CURRENT state — nothing from the flow is trusted.
        users = UserRepository(self._session, record.tenant_id)
        user = await users.get(record.user_id)
        if user is None or Role.ADMIN not in user.roles:
            return await _fail(REASON_DENIED, outcome=AuditOutcome.DENIED, detail="not_admin")
        sources = SourceRepository(self._session, record.tenant_id)
        source = await sources.get(record.source_id)
        if source is None or source.type != record.source_type:
            return await _fail(REASON_DENIED, outcome=AuditOutcome.DENIED, detail="source_gone")
        if source.connect_generation != record.generation:
            return await _fail(REASON_DENIED, outcome=AuditOutcome.DENIED, detail="superseded_flow")
        try:
            connector, spec = self._oauth_connector(source.type)
        except ConflictError:
            return await _fail(
                REASON_DENIED, outcome=AuditOutcome.DENIED, detail="oauth_not_supported"
            )

        # 3. Provider outcome: an error param (or missing code) is a provider
        #    failure — authenticated, audited, no exchange attempted.
        if error or not code:
            return await _fail(
                REASON_PROVIDER_ERROR,
                outcome=AuditOutcome.ERROR,
                detail="provider_error",
            )

        # 4. Exchange server-side (PKCE verifier attached). No secret is written
        #    on any failure.
        try:
            token = await exchange_code(
                self._http,
                spec,
                code=code,
                redirect_uri=record.redirect_uri,
                code_verifier=record.code_verifier,
            )
        except OAuthExchangeError:
            return await _fail(
                REASON_PROVIDER_ERROR,
                outcome=AuditOutcome.ERROR,
                detail="exchange_failed",
            )

        # 5. Persist the refresh token into the vault + bind the source. A
        #    provider may omit the refresh token on re-consent; that is fine
        #    only when the source already holds one (rotation-less reauthorize).
        secret_ref = source.auth_secret_ref
        if token.refresh_token is not None:
            secrets = build_secrets_service(
                self._session,
                settings=self._settings,
                tenant_id=record.tenant_id,
                owner_id=record.user_id,
                roles=(Role.ADMIN,),
                audit=audit,
                request_id=self._request_id,
                source_ip=self._source_ip,
            )
            ref = await secrets.store_secret(
                name=f"{_SECRET_NAME_PREFIX}{record.source_id}",
                kind=SecretKind.CONNECTOR_OAUTH,
                plaintext=token.refresh_token,
            )
            secret_ref = ref.id
        if secret_ref is None:
            return await _fail(
                REASON_PROVIDER_ERROR,
                outcome=AuditOutcome.ERROR,
                detail="no_refresh_token",
            )

        # 6. Provider-verified account identity (optional capability) + the
        #    auto-attestation of the connecting admin (ADR-0019 §2).
        email = await self._probe_account_email(connector, token)
        connected_account: dict[str, object] | None = (
            {"email": email} if email is not None else None
        )
        if (
            email is not None
            and email.casefold() == user.email.casefold()
            and user.email_attested_at is None
        ):
            attested = await users.attest_email(user.id, attested_by=user.id)
            if attested is not None:
                await audit.emit(
                    action=AuditAction.USER_IDENTITY_ATTESTED,
                    actor=AuditActor.system(),
                    resource_type="user",
                    resource_id=str(user.id),
                    outcome=AuditOutcome.ALLOWED,
                    request_id=self._request_id,
                    source_ip=self._source_ip,
                    metadata={"basis": "provider_verified_oauth"},
                )

        updated = await sources.complete_connect(
            record.source_id,
            auth_secret_ref=secret_ref,
            connected_account=connected_account or {},
        )
        assert updated is not None  # loaded above in this transaction  # noqa: S101

        await audit.emit(
            action=AuditAction.SOURCE_CONNECTED,
            actor=AuditActor.user(record.user_id),
            resource_type="source",
            resource_id=str(record.source_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "type": source.type,
                "email": email,
                "scopes": token.scope,
            },
        )
        self._enqueue_sync_after_commit(record.tenant_id, record.source_id)
        return self._success_url(record.source_id)

    async def _probe_account_email(self, connector: Connector, token: TokenResponse) -> str | None:
        """The connector's provider-verified account email, when it offers one.

        The optional ``fetch_account_email`` capability (ADR-0019 §1 — for
        Drive, an authenticated ``about.get`` after the exchange, because the
        ``drive.readonly`` code exchange itself returns no identity claim). A
        probe failure yields ``None`` — the account shows unverified, never a
        crashed callback.
        """
        probe = getattr(connector, "fetch_account_email", None)
        if probe is None or not callable(probe):
            return None
        try:
            email = await probe(self._http, token.access_token)
        except Exception as exc:  # noqa: BLE001 — identity probe is best-effort
            log.warning("connector_oauth.account_probe_failed", error=type(exc).__name__)
            return None
        return email if isinstance(email, str) and email else None

    def _enqueue_sync_after_commit(self, tenant_id: UUID, source_id: UUID) -> None:
        """First-sync enqueue, after the callback transaction commits.

        Mirrors ``SourcesService._enqueue_sync_after_commit`` (the single
        enqueue seam + off-loop dispatch, #271); never fires on rollback.
        """
        from sqlalchemy import event

        import app.tasks as tasks

        def _on_commit(_session: object) -> None:
            _dispatch_off_loop(
                lambda: tasks.enqueue_source_sync(tenant_id, source_id),
                name="enqueue-source-sync",
            )

        event.listen(self._session.sync_session, "after_commit", _on_commit, once=True)


__all__ = [
    "CALLBACK_PATH",
    "ConnectorOAuthService",
    "REASON_DENIED",
    "REASON_EXPIRED",
    "REASON_FAILED",
    "REASON_PROVIDER_ERROR",
]
