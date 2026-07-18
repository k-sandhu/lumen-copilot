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

from collections.abc import Callable
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
    build_authenticated_client,
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

# The per-source vault handle used at FIRST connect; later reconnects rotate
# the credential **by reference** (``rotate_secret_value``) so the row survives
# a different admin reconnecting (ADR-0019 §1).
_SECRET_NAME_PREFIX = "connector_oauth:"

# Bounded budget for the optional account-identity probe at callback time.
_PROBE_TIMEOUT_SECONDS = 15.0


class AccountEmailProbe(Protocol):
    """A connector's optional identity probe (ADR-0019 §1 — Drive: ``about.get``).

    Receives ONLY the framework-built, host-pinned authenticated client
    (ADR-0019 §4) — never a token string.
    """

    async def __call__(self, http: httpx.AsyncClient) -> str | None: ...


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
        probe_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._states = state_store
        self._http = token_http
        self._request_id = request_id
        self._source_ip = source_ip
        # The identity probe's inner transport (test seam — a MockTransport);
        # None ⇒ the real transport, always wrapped by the host-allowlist guard.
        self._probe_transport = probe_transport
        # The flow record of the CURRENT callback once state resolves — the
        # catch-all failure handler uses it for tenant-attributed audit.
        self._resolved_record: OAuthFlowRecord | None = None
        # The one-shot after_commit enqueue listener armed by a successful
        # flow. A ROLLBACK does not remove it, so every failure path that
        # later commits on this session (the failure audits) must disarm it
        # first — or the rolled-back flow's first sync would still fire.
        self._armed_enqueue: Callable[..., object] | None = None

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

    async def record_route_commit_failure(self) -> str:
        """The route's commit itself failed — audit it, return the failed URL.

        Called by the callback route after ITS ``commit()`` raised and rolled
        back: whatever the flow did is gone, but the failure still deserves a
        tenant-attributed ``source.connected`` ``outcome=error`` row (INV-6)
        when the state had resolved. Emitted+committed on the now-clean
        session; a failure of even this audit write is logged, never raised —
        the 302 guarantee outranks it.
        """
        # The failed route commit rolled the flow back but did NOT deregister
        # a successful flow's after_commit enqueue listener — disarm it before
        # this handler's own commit, or the rolled-back flow's sync would fire.
        self._disarm_enqueue_listener()
        record = self._resolved_record
        if record is not None:
            try:
                await bind_tenant(self._session, record.tenant_id)
                await self._audit(record.tenant_id).emit(
                    action=AuditAction.SOURCE_CONNECTED,
                    actor=AuditActor.user(record.user_id),
                    resource_type="source",
                    resource_id=str(record.source_id),
                    outcome=AuditOutcome.ERROR,
                    request_id=self._request_id,
                    source_ip=self._source_ip,
                    metadata={"reason": "commit_failed"},
                )
                await self._session.commit()
            except Exception:  # noqa: BLE001 — the 302 outranks the audit write
                log.exception("connector_oauth.commit_failure_audit_failed")
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
        self._resolved_record = None
        try:
            return await self._complete(state=state, code=code, error=error)
        except Exception:  # noqa: BLE001 — the callback must never raise
            log.exception("connector_oauth.callback_failed")
            # Roll back FIRST: by this point a secret, an attestation, or a
            # source mutation may already be flushed on the session — the
            # route's commit must never persist a half-completed flow (an
            # orphan credential behind a `connect=error` redirect).
            try:
                await self._session.rollback()
            except Exception:  # noqa: BLE001 — rollback is best-effort here
                log.exception("connector_oauth.callback_rollback_failed")
            # A successful flow may have armed the first-sync listener before
            # the exception: the failure-audit commit below must not fire it.
            self._disarm_enqueue_listener()
            # With a clean transaction, record the failure for the resolved
            # tenant (INV-6) — attribution exists only when state resolved.
            record = self._resolved_record
            if record is not None:
                try:
                    await bind_tenant(self._session, record.tenant_id)
                    await self._audit(record.tenant_id).emit(
                        action=AuditAction.SOURCE_CONNECTED,
                        actor=AuditActor.user(record.user_id),
                        resource_type="source",
                        resource_id=str(record.source_id),
                        outcome=AuditOutcome.ERROR,
                        request_id=self._request_id,
                        source_ip=self._source_ip,
                        metadata={"reason": "internal_error"},
                    )
                    await self._session.commit()
                except Exception:  # noqa: BLE001 — the 302 outranks the audit write
                    log.exception("connector_oauth.callback_failure_audit_failed")
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
        self._resolved_record = record

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

        # 5. Provider-verified account identity (optional capability) — the
        #    probe runs over a framework-built, guarded authenticated client;
        #    connector code never receives the token string (ADR-0019 §4).
        email = await self._probe_account_email(connector, spec, token)

        # 6. Vault plan — NOTHING an already-bound credential depends on is
        #    written before the CAS (a losing flow must not be able to alter
        #    the credential a live binding uses):
        #    * first-ever connect: a NEW row is created now (invisible to any
        #      other binding; unwound in this same transaction if the CAS
        #      loses);
        #    * reconnect/reauthorize: the existing row is left untouched here —
        #      its in-place, by-reference rotation happens ONLY after a CAS
        #      win (step 7), so a superseded flow can never rotate the shared
        #      credential from under the current one. A provider may omit the
        #      refresh token on re-consent; that is fine only when the source
        #      already holds one.
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
        existing_ref = source.auth_secret_ref
        if existing_ref is None and token.refresh_token is None:
            return await _fail(
                REASON_PROVIDER_ERROR,
                outcome=AuditOutcome.ERROR,
                detail="no_refresh_token",
            )
        secret_ref = existing_ref
        created_secret_id: UUID | None = None
        if existing_ref is None:
            assert token.refresh_token is not None  # guarded above  # noqa: S101
            ref = await secrets.store_secret(
                name=f"{_SECRET_NAME_PREFIX}{record.source_id}",
                kind=SecretKind.CONNECTOR_OAUTH,
                plaintext=token.refresh_token,
            )
            secret_ref = ref.id
            created_secret_id = ref.id
        assert secret_ref is not None  # both branches above set it  # noqa: S101

        # 7. Finalize with a CAS (ADR-0019 §1): one atomic UPDATE guarded on
        #    the flow's exact generation and a connectable status — the single
        #    authority on races. A flow superseded (or a source deleted /
        #    mid-sync) after the earlier checks loses here; the loser unwinds
        #    any row it created, rotates NOTHING, and reports `denied`. The
        #    acting user's admin role is re-read with an identity-map bypass
        #    (``refresh=True``) immediately before the CAS, so a demotion
        #    committed after the earlier check is seen, not masked by the
        #    session cache.
        fresh_user = await users.get(record.user_id, refresh=True)
        if fresh_user is None or Role.ADMIN not in fresh_user.roles:
            if created_secret_id is not None:
                await secrets.delete_secret(created_secret_id)
            return await _fail(REASON_DENIED, outcome=AuditOutcome.DENIED, detail="not_admin")
        connected_account: dict[str, object] = {"email": email} if email is not None else {}
        updated = await sources.complete_connect(
            record.source_id,
            expected_generation=record.generation,
            auth_secret_ref=secret_ref,
            connected_account=connected_account,
        )
        if updated is None:
            if created_secret_id is not None:
                await secrets.delete_secret(created_secret_id)
            return await _fail(REASON_DENIED, outcome=AuditOutcome.DENIED, detail="superseded_flow")

        # CAS won: NOW rotate an existing credential in place (same
        # transaction — a failure here raises into the catch-all, which rolls
        # the whole flow back including the CAS, so a half-rotated state can
        # never commit).
        if created_secret_id is None and token.refresh_token is not None:
            await secrets.rotate_secret_value(
                secret_ref,
                plaintext=token.refresh_token,
                accessor=AuditActor.user(record.user_id),
            )

        # 8. The auto-attestation (after the CAS so a losing flow attests
        #    nothing) + the success audit + the first sync.
        if (
            email is not None
            and email.casefold() == fresh_user.email.casefold()
            and fresh_user.email_attested_at is None
        ):
            attested = await users.attest_email(fresh_user.id, attested_by=fresh_user.id)
            if attested is not None:
                await audit.emit(
                    action=AuditAction.USER_IDENTITY_ATTESTED,
                    actor=AuditActor.system(),
                    resource_type="user",
                    resource_id=str(fresh_user.id),
                    outcome=AuditOutcome.ALLOWED,
                    request_id=self._request_id,
                    source_ip=self._source_ip,
                    metadata={"basis": "provider_verified_oauth"},
                )

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

    async def _probe_account_email(
        self, connector: Connector, spec: OAuthSpec, token: TokenResponse
    ) -> str | None:
        """The connector's provider-verified account email, when it offers one.

        The optional ``fetch_account_email`` capability (ADR-0019 §1 — for
        Drive, an authenticated ``about.get`` after the exchange, because the
        ``drive.readonly`` code exchange itself returns no identity claim). The
        probe receives a **framework-built, host-pinned authenticated client**
        (ADR-0019 §4) — never the token string; an off-allowlist request raises
        inside the guard before any credential leaves. A probe failure yields
        ``None`` — the account shows unverified, never a crashed callback.
        """
        probe = getattr(connector, "fetch_account_email", None)
        if probe is None or not callable(probe):
            return None
        try:
            async with build_authenticated_client(
                spec,
                access_token=token.access_token,
                timeout=_PROBE_TIMEOUT_SECONDS,
                inner_transport=self._probe_transport,
            ) as guarded:
                email = await probe(guarded)
        except Exception as exc:  # noqa: BLE001 — identity probe is best-effort
            log.warning("connector_oauth.account_probe_failed", error=type(exc).__name__)
            return None
        return email if isinstance(email, str) and email else None

    def _enqueue_sync_after_commit(self, tenant_id: UUID, source_id: UUID) -> None:
        """First-sync enqueue, after the callback transaction commits.

        Mirrors ``SourcesService._enqueue_sync_after_commit`` (the single
        enqueue seam + off-loop dispatch, #271); never fires on rollback — and
        the registered listener is retained so a failure path that commits
        AFTER a rollback (the failure audits) can disarm it first.
        """
        from sqlalchemy import event

        import app.tasks as tasks

        def _on_commit(_session: object) -> None:
            _dispatch_off_loop(
                lambda: tasks.enqueue_source_sync(tenant_id, source_id),
                name="enqueue-source-sync",
            )

        event.listen(self._session.sync_session, "after_commit", _on_commit, once=True)
        self._armed_enqueue = _on_commit

    def _disarm_enqueue_listener(self) -> None:
        """Remove a still-armed after_commit enqueue listener (fail closed).

        Rollback does NOT deregister ``after_commit`` listeners; without this,
        a failure-audit commit on the same session would publish the rolled-
        back flow's first sync. Removal of an already-fired/absent listener is
        a no-op.
        """
        armed = self._armed_enqueue
        if armed is None:
            return
        self._armed_enqueue = None
        from sqlalchemy import event

        try:
            event.remove(self._session.sync_session, "after_commit", armed)
        except Exception:  # noqa: BLE001 — already fired/removed is fine
            log.debug("connector_oauth.enqueue_listener_already_gone")


__all__ = [
    "CALLBACK_PATH",
    "ConnectorOAuthService",
    "REASON_DENIED",
    "REASON_EXPIRED",
    "REASON_FAILED",
    "REASON_PROVIDER_ERROR",
]
