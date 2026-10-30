"""Per-tenant LLM provider registration + model auto-discovery (foundation PR).

The orchestration layer for per-tenant LLM provider registration (ADR-0004:
``services/`` compose adapters; routers call exactly one service). A tenant ADMIN
registers an OpenAI-compatible provider (name + base URL + API key); the backend
auto-discovers its models by calling ``GET {base_url}/models`` and stores a
snapshot plus independent validation evidence (spec 0029). The picker and routing
enforce readiness through the single predicate in ``services/provider_models.py``.

The closest precedent is the MCP-server registration service
(:mod:`app.services.mcp_servers_service`) — per-tenant registration + a CC-C secret
ref + auto-discovery + a health status machine — and this mirrors it. It pairs:

* the tenant-scoped ``db/`` :class:`~app.db.repositories.LlmProviderRepository` — the
  only SQL for ``llm_providers``;
* the CC-C secrets vault (:class:`~app.services.secrets_service.SecretsService`,
  issue #209) — the API key is stored **write-only** (``store_secret`` returns a ref
  + masked hint, never the value) and resolved in-process at discovery time
  (``get_secret_plaintext``); the row keeps only ``api_key_secret_ref`` +
  ``secret_hint``. **SecretKind:** an LLM provider key reuses the existing
  :data:`~app.domain.entities.SecretKind.OTHER` kind rather than adding a new kind —
  a new ``SecretKind`` value would require a CHECK-constraint migration on the
  ``secrets`` table (``ck_secrets_kind``), and this PR is kept fully additive (no
  change to the secrets schema). ``OTHER`` is the vault's forward-compatible
  catch-all for exactly this case.
* the shared SSRF egress guard (:mod:`app.net.egress`) — the same https-only + range
  check + IP-pin discipline MCP uses, applied before a row is written and again on
  every discovery;
* the one ``AuditSink`` (spec 0004 §2.4) — ``llm_provider.created`` / ``updated`` /
  ``deleted`` / ``discovered`` (INV-6).

**Admin-gating + tenancy (spec 0004 §2.1/§2.3, INV-1/INV-5 — deny by default).**
Every route lives under the ``/admin`` router, which is gated by
``require_roles(Role.ADMIN)`` — a non-admin never reaches this service (403). An LLM
provider is tenant-wide admin config (any tenant admin manages any provider in the
tenant), so this service scopes to the caller's tenant (the repository) but does not
add a per-owner ownership check the way MCP does; ``owner_id`` records the admin who
registered it. A provider in another tenant is treated as **non-existent**: the
read/op returns ``None`` and the router maps that to **404** (existence
non-disclosure; never 403). ``tenant_id``/``owner_id`` come from the resolved
principal, never from request input.

**SSRF (load-bearing).** ``create``/``update`` validate the base URL before a row is
written: https-only + the shared range check (:mod:`app.net.egress`) — a non-https,
unresolvable, or blocked (loopback / private / link-local / CGNAT / cloud-metadata)
URL raises a typed ``ValidationError`` (``base_url_blocked`` / ``base_url_scheme``) →
**422**. The full guard (resolve-all + IP-pin) runs again at discovery time.

**Discovery is a recorded state, never a 500.** A bad key/url/unreachable host does
not raise — discovery sets ``status=error`` + a fixed safe ``last_error`` /
``last_error_code`` and persists it. Validation is evidence-based (spec 0029):
discovery proves at most ``reachable``; only the explicit bounded test-connection
proves authentication + chat capability and yields ``ready``. The credential is
never returned or echoed — no method, wire shape, audit event, or log here exposes
the stored API-key value or any fragment of it, only its presence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.repositories import LlmProviderRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AuditOutcome,
    LlmProvider,
    LlmProviderStatus,
    LlmProviderType,
    Role,
    SecretKind,
)
from app.llm.provider_catalog import ProviderProbeError, discover_models
from app.llm.provider_catalog import (
    test_connection as probe_test_connection,
)
from app.net.egress import EgressBlockedError, resolve_safe_ip
from app.services.audit import AuditSink, PermissionDeniedContext, audited_resource
from app.services.secrets_service import SecretsService, build_secrets_service

log = get_logger(__name__)

# LLM providers carry an API key, so — like the MCP adapter's egress guard — only
# https is accepted for a remote base URL (credentials in transit).
_ALLOWED_SCHEME = "https"

# The stable name under which a provider's API key is stored in the CC-C vault
# (per-provider singleton keyed on this + the provider id). A fixed prefix so a
# rotation re-stores in place and a delete can find it.
_SECRET_NAME_PREFIX = "llm-provider-key"

# The API key is stored under the vault's forward-compatible ``OTHER`` kind (see the
# module docstring — a new kind would need a ``secrets`` CHECK migration; this PR is
# additive).
_SECRET_KIND = SecretKind.OTHER

# A cap on how many discovered models we persist, so a pathological provider cannot
# blow up the row / response. OpenAI-compatible catalogs are well under this.
_MAX_MODELS = 1000


@dataclass(frozen=True, slots=True)
class ProviderValidationOutcome:
    """One validation run's result (contract ``ProviderValidationResult``).

    Carries only safe values: the new status, the independent checks, the
    failed stage + stable code, retryability, and the tested model. The router
    adds the request correlation id. Never exception text (spec 0029).
    """

    status: LlmProviderStatus
    checks: list[dict[str, object]]
    stage: str | None
    code: str | None
    retryable: bool
    tested_model: str | None
    validated_at: datetime

    def as_record(self, correlation_id: str) -> dict[str, object]:
        return {
            "status": self.status.value,
            "checks": self.checks,
            "stage": self.stage,
            "code": self.code,
            "retryable": self.retryable,
            "correlation_id": correlation_id,
            "tested_model": self.tested_model,
            "validated_at": self.validated_at.isoformat(),
        }


class LlmProviderService:
    """Register / update / delete / discover per-tenant LLM providers (foundation PR).

    Constructed per-request with the session, the resolved ``tenant_id`` +
    ``owner_id`` + the caller's ``roles`` (admin, from the token — never request
    input), the CC-C :class:`SecretsService`, an injectable ``discovery_client``
    (production builds a fresh SSRF-guarded :class:`httpx.AsyncClient`; a test injects
    one over a mock transport so no socket is opened), the discovery timeout + user
    agent, and the audit sink + correlation context. All tenancy enforcement lives
    here; the router only (de)serialises and maps ``None`` → 404.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        roles: tuple[Role, ...],
        secrets: SecretsService,
        audit: AuditSink,
        denials: PermissionDeniedContext,
        request_id: str,
        source_ip: str,
        user_agent: str,
        discovery_timeout_seconds: float = 15.0,
        discovery_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._providers = LlmProviderRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._is_admin = Role.ADMIN in roles
        self._secrets = secrets
        self._audit = audit
        self._denials = denials
        self._denials.assert_user(tenant_id, owner_id)
        self._request_id = request_id
        self._source_ip = source_ip
        self._user_agent = user_agent
        self._discovery_timeout_seconds = discovery_timeout_seconds
        self._discovery_client = discovery_client

    # --- internal helpers ---------------------------------------------------

    def _parse_provider_type(self, provider_type: str) -> LlmProviderType:
        """Resolve a wire provider-type string to the domain enum, or 422.

        Only ``openai_compatible`` ships in this PR (the enum is small but
        extensible); anything else is ``unsupported_provider_type`` (422).
        """
        try:
            return LlmProviderType(provider_type)
        except ValueError as exc:
            raise ValidationError(
                f"unsupported LLM provider type {provider_type!r} "
                "(only 'openai_compatible' is supported)",
                code="unsupported_provider_type",
            ) from exc

    def _validate_base_url(self, base_url: str) -> None:
        """https-only + SSRF pre-check before a row is written.

        The register/discover chokepoint's synchronous half: refuse a non-https
        scheme or a missing host, then resolve-all/reject-any via the shared egress
        predicate (loopback / private / link-local / CGNAT / cloud-metadata). A
        rejection is a 422 whose ``code`` distinguishes the case
        (``base_url_scheme`` / ``base_url_blocked``, INV-8). The discovery path
        re-runs the full guard (incl. IP-pin) at fetch time — this is the pre-write
        gate, not the only one.
        """
        try:
            parts = urlsplit(base_url)
            port = parts.port
        except ValueError:
            raise ValidationError("The base URL is not allowed.", code="base_url_blocked") from None
        scheme = parts.scheme.lower()
        if scheme != _ALLOWED_SCHEME:
            raise ValidationError(
                "The base URL must use https.",
                code="base_url_scheme",
            )
        host = parts.hostname
        if not host:
            raise ValidationError("LLM provider base URL has no host.", code="base_url_scheme")
        if (
            parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or port == 0
        ):
            raise ValidationError("The base URL is not allowed.", code="base_url_blocked")
        try:
            resolve_safe_ip(host)
        except EgressBlockedError:
            # Generic detail only: the egress message carries the resolved
            # address, which must never reach the client (spec 0029). The
            # stable code still distinguishes the case.
            raise ValidationError("The base URL is not allowed.", code="base_url_blocked") from None

    async def _require_admin(self, *, attempted_action: str, resource_id: str) -> None:
        if self._is_admin:
            return
        await self._denials.emit(
            resource_type="llm_provider",
            resource_id=resource_id,
            attempted_action=attempted_action,
            reason="missing_required_role",
            required_roles=(Role.ADMIN.value,),
        )
        raise ForbiddenError("LLM provider management requires the admin role.")

    async def _visible(
        self, provider_id: UUID, *, attempted_action: str, for_update: bool = False
    ) -> LlmProvider | None:
        """Fetch a provider in the caller's tenant, or ``None`` (→ 404).

        ``None`` for a missing id or a foreign-tenant id (the repository sees no
        row) — INV-1 collapses both to 404 at the router. Admin-gating is the
        router's; every tenant admin may manage any provider in the tenant.
        """
        provider = await self._providers.get(provider_id, for_update=for_update)
        if provider is None:
            await self._denials.emit(
                resource_type="llm_provider",
                resource_id=str(provider_id),
                attempted_action=attempted_action,
                reason="not_visible",
            )
        return provider

    async def _store_api_key(self, provider_id: UUID, api_key: str) -> tuple[UUID, str]:
        """Store the write-only API key via CC-C; return ``(secret_id, hint)``.

        Envelope-encrypted in the vault under a per-provider singleton name (so a
        rotation re-stores in place); ``store_secret`` returns a plaintext-free ref
        carrying only the masked hint. The value never touches this row, log, or
        response.
        """
        ref = await self._secrets.store_secret(
            name=f"{_SECRET_NAME_PREFIX}:{provider_id}",
            kind=_SECRET_KIND,
            plaintext=api_key,
            include_hint=False,
        )
        return ref.id, ref.hint

    async def _delete_api_key_secret(self, api_key_secret_ref: str) -> None:
        """Best-effort delete of the CC-C vault secret backing a provider's key.

        A rotation/delete removes the old secret so the vault does not accumulate
        orphans. A missing/foreign secret is a 404 inside the vault (idempotent from
        our view) — swallowed so a stale ref never blocks the provider mutation.
        """
        try:
            await self._secrets.delete_secret(UUID(api_key_secret_ref))
        except Exception:  # noqa: BLE001 — a stale/foreign ref must not block the mutation
            log.warning("llm_provider.api_key_secret_delete_failed", ref=api_key_secret_ref)

    async def _resolve_api_key(self, api_key_secret_ref: str | None) -> str | None:
        """Resolve a CC-C secret ref to its plaintext (in-process, never returned/logged).

        Given a provider's ``api_key_secret_ref``, decrypt the key from the vault at
        discovery time (a ``secret.accessed`` audit records the read, never the
        value). Returns ``None`` for a missing/malformed ref (an anonymous provider).
        """
        if api_key_secret_ref is None:
            return None
        try:
            secret_id = UUID(api_key_secret_ref)
        except ValueError:
            raise ValidationError("Provider credential is unavailable.") from None
        return await self._secrets.get_secret_plaintext(secret_id, accessor=AuditActor.system())

    # --- discovery ----------------------------------------------------------

    async def _discover_models(
        self, *, base_url: str, api_key: str | None
    ) -> list[dict[str, object]]:
        """Discover the provider's model catalog via the ``llm/`` boundary (ADR-0004).

        The OpenAI-compatible ``GET {base_url}/models`` HTTP call — with the SSRF guard
        and bearer auth — lives in :mod:`app.llm.provider_catalog`; this service only
        orchestrates (resolve the key, delegate, persist). Raises on any failure so the
        caller records ``status=error`` (never a 500).
        """
        return await discover_models(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=self._discovery_timeout_seconds,
            user_agent=self._user_agent,
            http_client=self._discovery_client,
        )

    async def _run_discovery(self, provider: LlmProvider) -> LlmProvider:
        """Discover + persist a provider's models; record reachable/error, never raise.

        Resolves the API key in-process from CC-C (never returned/logged), calls
        ``GET {base_url}/models`` through the SSRF guard, and on success persists
        ``status=reachable`` (spec 0029: catalog evidence only — authentication
        stays unverified, so discovery alone never yields ``ready``); on ANY
        failure persists ``status=error`` + the fixed safe message/code and
        preserves the previous model snapshot. Audits
        ``llm_provider.discovered`` (INV-6) either way.
        """
        probe_error: ProviderProbeError | None = None
        models: list[dict[str, object]] = []
        try:
            api_key = await self._resolve_api_key(provider.api_key_secret_ref)
            models = await self._discover_models(base_url=provider.base_url, api_key=api_key)
        except ProviderProbeError as exc:
            probe_error = exc
        except Exception:  # noqa: BLE001 — a bad provider is a state, not a 500
            probe_error = ProviderProbeError(stage="discovery", code="validation_failed")
        has_key = provider.api_key_secret_ref is not None
        now = _utc_now()
        checks = _discovery_checks(has_key=has_key, error=probe_error)
        outcome = ProviderValidationOutcome(
            status=LlmProviderStatus.REACHABLE if probe_error is None else LlmProviderStatus.ERROR,
            checks=checks,
            stage=None if probe_error is None else probe_error.stage,
            code=None if probe_error is None else probe_error.code,
            retryable=False if probe_error is None else probe_error.retryable,
            tested_model=None,
            validated_at=now,
        )
        if probe_error is not None:
            log.info(
                "llm_provider.discovery_failed",
                provider_id=str(provider.id),
                stage=probe_error.stage,
                code=probe_error.code,
                retryable=probe_error.retryable,
                request_id=self._request_id,
            )

        if probe_error is None:
            updated = await self._providers.set_discovery(
                provider.id,
                status=LlmProviderStatus.REACHABLE,
                discovered_models=models,
                last_error=None,
                last_discovery_at=now,
                validation_checks=checks,
                last_error_code=None,
                validated_at=now,
                validation_result=outcome.as_record(self._request_id),
            )
        else:
            updated = await self._providers.set_discovery(
                provider.id,
                status=LlmProviderStatus.ERROR,
                discovered_models=provider.discovered_models,
                last_error=probe_error.safe_message,
                last_discovery_at=provider.last_discovery_at,
                validation_checks=checks,
                last_error_code=probe_error.code,
                validated_at=now,
                validation_result=outcome.as_record(self._request_id),
            )
        if updated is None:
            raise NotFoundError("LLM provider not found.")

        metadata: dict[str, object] = {
            "status": updated.status.value,
            "model_count": len(updated.discovered_models),
        }
        if probe_error is not None:
            metadata["error_code"] = probe_error.code
        await self._audit.emit(
            action=AuditAction.LLM_PROVIDER_DISCOVERED,
            actor=AuditActor.user(self._owner_id),
            resource_type="llm_provider",
            resource_id=str(provider.id),
            outcome=AuditOutcome.ALLOWED if probe_error is None else AuditOutcome.ERROR,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata=metadata,
        )
        return updated

    @audited_resource(
        "llm_provider.test_connection", "llm_provider", "provider_id", missing_result=True
    )
    async def test_connection(
        self, provider_id: UUID, *, model: str
    ) -> tuple[LlmProvider, ProviderValidationOutcome] | None:
        """Send ONE minimal bounded chat request; persist ready/error (spec 0029).

        The only path to ``ready``: the admin-selected discovered ``model``
        receives a single ``max_tokens=1`` request (the provider may bill for
        it — the UI states this). Success proves authentication + chat
        capability, so every required check passes. Failure records
        ``status=error`` with the stable safe code. An unknown ``model`` is a
        422 (``test_model_unknown``) sent before any network I/O. ``None``
        when not visible (→ 404). Audits ``llm_provider.connection_tested``.
        """
        await self._require_admin(
            attempted_action="llm_provider.test_connection", resource_id=str(provider_id)
        )
        provider = await self._visible(
            provider_id, attempted_action="llm_provider.test_connection", for_update=True
        )
        if provider is None:
            return None
        known = {
            str(m.get("id"))
            for m in provider.discovered_models
            if isinstance(m.get("id"), str) and m.get("id")
        }
        if not any(
            c.get("name") == "catalog_discovered" and c.get("status") == "passed"
            for c in provider.validation_checks
        ):
            known = set()
        if model not in known:
            raise ValidationError(
                "Select a model from the provider's discovered catalog.",
                code="test_model_unknown",
            )

        probe_error: ProviderProbeError | None = None
        try:
            api_key = await self._resolve_api_key(provider.api_key_secret_ref)
            await probe_test_connection(
                base_url=provider.base_url,
                api_key=api_key,
                model=model,
                timeout_seconds=self._discovery_timeout_seconds,
                user_agent=self._user_agent,
                http_client=self._discovery_client,
            )
        except ProviderProbeError as exc:
            probe_error = exc
        except Exception:  # noqa: BLE001 — a bad provider is a state, not a 500
            probe_error = ProviderProbeError(stage="connection_test", code="validation_failed")
        has_key = provider.api_key_secret_ref is not None
        now = _utc_now()
        checks = _test_checks(has_key=has_key, error=probe_error)
        status = LlmProviderStatus.READY if probe_error is None else LlmProviderStatus.ERROR
        outcome = ProviderValidationOutcome(
            status=status,
            checks=checks,
            stage=None if probe_error is None else probe_error.stage,
            code=None if probe_error is None else probe_error.code,
            retryable=False if probe_error is None else probe_error.retryable,
            tested_model=model,
            validated_at=now,
        )
        if probe_error is not None:
            log.info(
                "llm_provider.connection_test_failed",
                provider_id=str(provider.id),
                stage=probe_error.stage,
                code=probe_error.code,
                retryable=probe_error.retryable,
                request_id=self._request_id,
            )
        updated = await self._providers.set_discovery(
            provider.id,
            status=status,
            discovered_models=provider.discovered_models,
            last_error=None if probe_error is None else probe_error.safe_message,
            last_discovery_at=provider.last_discovery_at,
            validation_checks=checks,
            last_error_code=None if probe_error is None else probe_error.code,
            validated_at=now,
            validation_result=outcome.as_record(self._request_id),
        )
        if updated is None:
            raise NotFoundError("LLM provider not found.")
        metadata: dict[str, object] = {
            "status": updated.status.value,
            "tested_model": model,
        }
        if probe_error is not None:
            metadata["error_code"] = probe_error.code
        await self._audit.emit(
            action=AuditAction.LLM_PROVIDER_CONNECTION_TESTED,
            actor=AuditActor.user(self._owner_id),
            resource_type="llm_provider",
            resource_id=str(provider.id),
            outcome=AuditOutcome.ALLOWED if probe_error is None else AuditOutcome.ERROR,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata=metadata,
        )
        return updated, outcome

    # --- use-cases ----------------------------------------------------------

    async def list_providers(self) -> list[LlmProvider]:
        """Every registered provider in the caller's tenant (newest first, INV-1)."""
        await self._require_admin(attempted_action="llm_provider.list", resource_id="collection")
        return await self._providers.list_for_tenant()

    async def get(self, provider_id: UUID) -> LlmProvider | None:
        """Fetch one provider in the caller's tenant, or ``None`` (→ 404)."""
        await self._require_admin(
            attempted_action="llm_provider.read", resource_id=str(provider_id)
        )
        return await self._visible(provider_id, attempted_action="llm_provider.read")

    async def create(
        self,
        *,
        name: str,
        provider_type: str,
        base_url: str,
        api_key: str | None,
    ) -> LlmProvider:
        """Register an LLM provider, discover its models, and return it.

        Order (fail-closed):

        1. **Type** — resolve/validate (unsupported → 422).
        2. **Base URL** — https + SSRF pre-check *before* anything is written
           (non-https → 422 ``base_url_scheme``; blocked → 422 ``base_url_blocked``).
        3. **Row** — create ``status=pending``.
        4. **Key** — if supplied, store it write-only via CC-C and attach the ref +
           masked hint to the row (never the value).
        5. **Audit** ``llm_provider.created`` (INV-6) — key presence only.
        6. **Discover** — probe ``GET /models`` and persist reachable/error (never raises;
           a bad key/url is a recorded state, audited as ``llm_provider.discovered``).

        Raises:
            ValidationError: unsupported type, or an invalid / non-https / SSRF-blocked
                base URL — all mapped to **422** (INV-8).
        """
        await self._require_admin(attempted_action="llm_provider.create", resource_id="new")
        parsed = self._parse_provider_type(provider_type)
        self._validate_base_url(base_url)

        provider = await self._providers.create(
            owner_id=self._owner_id,
            name=name,
            provider_type=parsed.value,
            base_url=base_url,
            api_key_secret_ref=None,
            secret_hint=None,
        )

        if api_key:
            secret_id, hint = await self._store_api_key(provider.id, api_key)
            updated = await self._providers.update(
                provider.id, api_key_secret_ref=secret_id, secret_hint=hint
            )
            assert updated is not None  # just created  # noqa: S101
            provider = updated

        await self._audit.emit(
            action=AuditAction.LLM_PROVIDER_CREATED,
            actor=AuditActor.user(self._owner_id),
            resource_type="llm_provider",
            resource_id=str(provider.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "name": provider.name,
                "provider_type": provider.provider_type,
                # Presence only — no key-derived fragment in audit (spec 0029).
                "has_api_key": provider.api_key_secret_ref is not None,
            },
        )
        return await self._run_discovery(provider)

    @audited_resource("llm_provider.update", "llm_provider", "provider_id", missing_result=True)
    async def update(
        self,
        provider_id: UUID,
        *,
        name: str | None = None,
        base_url: str | None = None,
        enabled: bool | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> LlmProvider | None:
        """Update a provider (rename, retarget, toggle, rotate/clear key), or None.

        Tenant visibility is established before any write. Changing ``base_url``
        re-runs the https + SSRF validation (else 422). Rotating ``api_key`` stores
        the new value write-only via CC-C and replaces the ref + hint; ``clear_api_key``
        deletes the vault secret and nulls the ref + hint. The key value is never
        returned — only key presence is exposed. Re-runs discovery when the
        ``base_url`` or the ``api_key`` changed (a new target/credential needs a fresh
        model snapshot). Audits ``llm_provider.updated`` (INV-6). ``None`` when not
        visible (→ 404).
        """
        await self._require_admin(
            attempted_action="llm_provider.update", resource_id=str(provider_id)
        )
        provider = await self._visible(
            provider_id, attempted_action="llm_provider.update", for_update=True
        )
        if provider is None:
            return None

        if base_url is not None:
            self._validate_base_url(base_url)

        new_ref: UUID | None = None
        new_hint: str | None = None
        do_clear = False
        if clear_api_key:
            do_clear = True
            if provider.api_key_secret_ref is not None:
                await self._delete_api_key_secret(provider.api_key_secret_ref)
        elif api_key:
            new_ref, new_hint = await self._store_api_key(provider.id, api_key)

        updated = await self._providers.update(
            provider_id,
            name=name,
            base_url=base_url,
            enabled=enabled,
            api_key_secret_ref=new_ref,
            secret_hint=new_hint,
            clear_api_key=do_clear,
        )
        if updated is None:
            return None

        await self._audit.emit(
            action=AuditAction.LLM_PROVIDER_UPDATED,
            actor=AuditActor.user(self._owner_id),
            resource_type="llm_provider",
            resource_id=str(provider_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "name": updated.name,
                "provider_type": updated.provider_type,
                # Presence only — no key-derived fragment in audit (spec 0029).
                "has_api_key": updated.api_key_secret_ref is not None,
            },
        )

        # Re-discover when the target or credential changed (or was cleared) — a new
        # base URL / key needs a fresh model snapshot (mirrors MCP re-probing).
        if base_url is not None or api_key or clear_api_key:
            return await self._run_discovery(updated)
        return updated

    @audited_resource("llm_provider.refresh", "llm_provider", "provider_id", missing_result=True)
    async def refresh(self, provider_id: UUID) -> LlmProvider | None:
        """Re-run model discovery for a provider on demand; persist the outcome, or 404.

        The explicit refresh: probe ``GET {base_url}/models`` again through the SSRF
        guard, resolving the key in-process from CC-C. On success ``status=reachable``
        + a fresh snapshot (auth unverified until test-connection, spec 0029); on
        failure ``status=error`` + a safe ``last_error`` (previous snapshot preserved)
        — never raises (a bad provider is a recorded state).
        Audits ``llm_provider.discovered`` (INV-6). ``None`` when not visible (→ 404).
        """
        await self._require_admin(
            attempted_action="llm_provider.refresh", resource_id=str(provider_id)
        )
        provider = await self._visible(
            provider_id, attempted_action="llm_provider.refresh", for_update=True
        )
        if provider is None:
            return None
        return await self._run_discovery(provider)

    @audited_resource("llm_provider.delete", "llm_provider", "provider_id", missing_result=True)
    async def delete(self, provider_id: UUID) -> bool:
        """Delete a provider + its stored API key, else 404.

        Tenant visibility is established before any write. In one transaction: the
        CC-C vault secret (if any) is deleted, then the provider row. Audits
        ``llm_provider.deleted`` (INV-6). Returns ``False`` when not visible.
        """
        await self._require_admin(
            attempted_action="llm_provider.delete", resource_id=str(provider_id)
        )
        provider = await self._visible(
            provider_id, attempted_action="llm_provider.delete", for_update=True
        )
        if provider is None:
            return False

        if provider.api_key_secret_ref is not None:
            await self._delete_api_key_secret(provider.api_key_secret_ref)

        deleted = await self._providers.delete(provider_id)
        if not deleted:
            return False

        await self._audit.emit(
            action=AuditAction.LLM_PROVIDER_DELETED,
            actor=AuditActor.user(self._owner_id),
            resource_type="llm_provider",
            resource_id=str(provider_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"name": provider.name, "provider_type": provider.provider_type},
        )
        return True


# --- module helpers ---------------------------------------------------------


def _utc_now() -> datetime:
    """Current UTC timestamp (isolated so tests read one clock)."""
    return datetime.now(UTC)


# Probe codes that mean "no HTTP response was ever received" — the provider
# was not shown reachable. Every other code implies a response arrived.
_NO_RESPONSE_CODES = frozenset(
    {"blocked_address", "dns_failed", "timeout", "connection_failed", "validation_failed"}
)


def _check(name: str, status: str, code: str | None = None) -> dict[str, object]:
    return {"name": name, "status": status, "code": code}


def _auth_unverified(has_key: bool) -> dict[str, object]:
    return _check("auth_verified", "unverified" if has_key else "not_applicable")


def _discovery_checks(
    *, has_key: bool, error: ProviderProbeError | None
) -> list[dict[str, object]]:
    """Checks after a /models discovery run (catalog evidence only, spec 0029)."""
    if error is None:
        return [
            _check("config_valid", "passed"),
            _check("reachable", "passed"),
            _check("catalog_discovered", "passed"),
            _auth_unverified(has_key),
            _check("capabilities_verified", "unverified"),
        ]
    if error.code in _NO_RESPONSE_CODES:
        return [
            _check("config_valid", "failed", error.code)
            if error.code == "blocked_address"
            else _check("config_valid", "passed"),
            _check("reachable", "failed", error.code),
            _check("catalog_discovered", "unverified"),
            _auth_unverified(has_key),
            _check("capabilities_verified", "unverified"),
        ]
    return [
        _check("config_valid", "passed"),
        _check("reachable", "passed"),
        _check("catalog_discovered", "failed", error.code),
        _check("auth_verified", "failed", error.code)
        if error.code in {"unauthorized", "forbidden"}
        else _auth_unverified(has_key),
        _check("capabilities_verified", "unverified"),
    ]


def _test_checks(*, has_key: bool, error: ProviderProbeError | None) -> list[dict[str, object]]:
    """Checks after a bounded test-connection run (the required set, spec 0029)."""
    if error is None:
        return [
            _check("config_valid", "passed"),
            _check("reachable", "passed"),
            _check("catalog_discovered", "passed"),
            _check("auth_verified", "passed" if has_key else "not_applicable"),
            _check("capabilities_verified", "passed"),
        ]
    if error.code in {"unauthorized", "forbidden"}:
        return [
            _check("config_valid", "passed"),
            _check("reachable", "passed"),
            _check("catalog_discovered", "passed"),
            _check("auth_verified", "failed", error.code),
            _check("capabilities_verified", "unverified"),
        ]
    if error.code in _NO_RESPONSE_CODES:
        return [
            _check("config_valid", "failed", error.code)
            if error.code == "blocked_address"
            else _check("config_valid", "passed"),
            _check("reachable", "failed", error.code),
            _check("catalog_discovered", "passed"),
            _auth_unverified(has_key),
            _check("capabilities_verified", "unverified"),
        ]
    return [
        _check("config_valid", "passed"),
        _check("reachable", "passed"),
        _check("catalog_discovered", "passed"),
        _auth_unverified(has_key),
        _check("capabilities_verified", "failed", error.code),
    ]


def build_llm_provider_service(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: UUID,
    owner_id: UUID,
    roles: tuple[Role, ...],
    audit: AuditSink,
    denials: PermissionDeniedContext,
    request_id: str,
    source_ip: str,
    discovery_client: httpx.AsyncClient | None = None,
) -> LlmProviderService:
    """Assemble a :class:`LlmProviderService` from settings (the production wiring).

    Config is the single env reader (``core/config.py``); the caller passes a
    ``Settings`` rather than reading the environment. This factory builds the CC-C
    :class:`SecretsService` (via :func:`build_secrets_service`, the sole cipher
    importer — so no ``api/`` module ever touches the cipher/plaintext). The discovery
    HTTP client is left ``None`` in production (a fresh SSRF-guarded client is built
    per discovery); a test injects one over a mock transport so no socket is opened.
    The discovery timeout + outbound user agent come from the MCP config block (reused
    — the same egress discipline; no new config surface in this additive PR).
    """
    secrets = build_secrets_service(
        session,
        settings=settings,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        audit=audit,
        request_id=request_id,
        source_ip=source_ip,
    )
    return LlmProviderService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        secrets=secrets,
        audit=audit,
        denials=denials,
        request_id=request_id,
        source_ip=source_ip,
        user_agent=settings.web_user_agent,
        discovery_timeout_seconds=settings.mcp_connect_timeout_seconds,
        discovery_client=discovery_client,
    )


__all__ = [
    "LlmProviderService",
    "ProviderValidationOutcome",
    "build_llm_provider_service",
]
