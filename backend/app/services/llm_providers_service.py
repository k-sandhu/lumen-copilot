"""Per-tenant LLM provider registration + model auto-discovery (foundation PR).

The orchestration layer for per-tenant LLM provider registration (ADR-0004:
``services/`` compose adapters; routers call exactly one service). A tenant ADMIN
registers an OpenAI-compatible provider (name + base URL + API key); the backend
auto-discovers its models by calling ``GET {base_url}/models`` and stores a
snapshot + discovery/health status. **This is the model-catalog foundation only:
nothing here surfaces a provider in the chat model picker or routes a chat/
embedding completion through it — that is a separate follow-up PR.**

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
not raise — discovery sets ``status=error`` + a safe (truncated) ``last_error`` and
persists it, mirroring MCP's status handling. The credential is never returned — no
method or wire shape here exposes the stored API-key value, only the masked
``secret_hint``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ForbiddenError, ValidationError
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
from app.llm.provider_catalog import discover_models
from app.net.egress import EgressBlockedError, resolve_safe_ip
from app.services.audit import AuditSink, PermissionDeniedContext
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

# Truncation bound for a persisted ``last_error`` so a verbose upstream body can
# never bloat the row (mirrors MCP's safe, bounded failure reason).
_MAX_ERROR_LEN = 500

# A cap on how many discovered models we persist, so a pathological provider cannot
# blow up the row / response. OpenAI-compatible catalogs are well under this.
_MAX_MODELS = 1000


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
        self._denials.assert_user(owner_id)
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
        parts = urlsplit(base_url)
        scheme = parts.scheme.lower()
        if scheme != _ALLOWED_SCHEME:
            raise ValidationError(
                f"LLM provider base URL scheme {scheme or '(none)'!r} is not allowed; "
                "only https is permitted (credentials in transit)",
                code="base_url_scheme",
            )
        host = parts.hostname
        if not host:
            raise ValidationError("LLM provider base URL has no host.", code="base_url_scheme")
        try:
            resolve_safe_ip(host)
        except EgressBlockedError as exc:
            raise ValidationError(str(exc), code="base_url_blocked") from exc

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

    async def _visible(self, provider_id: UUID, *, attempted_action: str) -> LlmProvider | None:
        """Fetch a provider in the caller's tenant, or ``None`` (→ 404).

        ``None`` for a missing id or a foreign-tenant id (the repository sees no
        row) — INV-1 collapses both to 404 at the router. Admin-gating is the
        router's; every tenant admin may manage any provider in the tenant.
        """
        provider = await self._providers.get(provider_id)
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
            return None
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
        """Discover + persist a provider's models; record ready/error, never raise.

        Resolves the API key in-process from CC-C (never returned/logged), calls
        ``GET {base_url}/models`` through the SSRF guard, and on success persists
        ``status=ready`` + the fresh ``last_discovery_at`` + the model snapshot; on
        ANY failure (bad key/url, unreachable host, malformed response) persists
        ``status=error`` + a safe truncated ``last_error`` and preserves the previous
        model snapshot — a bad provider is a recorded state, not an exception (mirrors
        MCP). Audits ``llm_provider.discovered`` (INV-6) either way.
        """
        error: str | None = None
        models: list[dict[str, object]] = []
        try:
            api_key = await self._resolve_api_key(provider.api_key_secret_ref)
            models = await self._discover_models(base_url=provider.base_url, api_key=api_key)
        except Exception as exc:  # noqa: BLE001 — a bad provider is a state, not a 500
            error = _safe_error(exc)
            log.info(
                "llm_provider.discovery_failed",
                provider_id=str(provider.id),
                error=error,
            )

        if error is None:
            updated = await self._providers.set_discovery(
                provider.id,
                status=LlmProviderStatus.READY,
                discovered_models=models,
                last_error=None,
                last_discovery_at=_utc_now(),
            )
        else:
            updated = await self._providers.set_discovery(
                provider.id,
                status=LlmProviderStatus.ERROR,
                discovered_models=provider.discovered_models,
                last_error=error,
                last_discovery_at=provider.last_discovery_at,
            )
        assert updated is not None  # just fetched  # noqa: S101

        await self._audit.emit(
            action=AuditAction.LLM_PROVIDER_DISCOVERED,
            actor=AuditActor.user(self._owner_id),
            resource_type="llm_provider",
            resource_id=str(provider.id),
            outcome=AuditOutcome.ALLOWED if error is None else AuditOutcome.ERROR,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "status": updated.status.value,
                "model_count": len(updated.discovered_models),
            },
        )
        return updated

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
        5. **Audit** ``llm_provider.created`` (INV-6) — metadata + hint, never the key.
        6. **Discover** — probe ``GET /models`` and persist ready/error (never raises;
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
                "has_api_key": provider.api_key_secret_ref is not None,
                "secret_hint": provider.secret_hint,
            },
        )
        return await self._run_discovery(provider)

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
        returned — only the updated masked ``secret_hint``. Re-runs discovery when the
        ``base_url`` or the ``api_key`` changed (a new target/credential needs a fresh
        model snapshot). Audits ``llm_provider.updated`` (INV-6). ``None`` when not
        visible (→ 404).
        """
        await self._require_admin(
            attempted_action="llm_provider.update", resource_id=str(provider_id)
        )
        provider = await self._visible(provider_id, attempted_action="llm_provider.update")
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
        if updated is None:  # pragma: no cover — visibility already established
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
                "has_api_key": updated.api_key_secret_ref is not None,
                "secret_hint": updated.secret_hint,
            },
        )

        # Re-discover when the target or credential changed (or was cleared) — a new
        # base URL / key needs a fresh model snapshot (mirrors MCP re-probing).
        if base_url is not None or api_key or clear_api_key:
            return await self._run_discovery(updated)
        return updated

    async def refresh(self, provider_id: UUID) -> LlmProvider | None:
        """Re-run model discovery for a provider on demand; persist the outcome, or 404.

        The explicit refresh: probe ``GET {base_url}/models`` again through the SSRF
        guard, resolving the key in-process from CC-C. On success ``status=ready`` +
        a fresh snapshot; on failure ``status=error`` + a safe ``last_error`` (previous
        snapshot preserved) — never raises (a bad provider is a recorded state).
        Audits ``llm_provider.discovered`` (INV-6). ``None`` when not visible (→ 404).
        """
        await self._require_admin(
            attempted_action="llm_provider.refresh", resource_id=str(provider_id)
        )
        provider = await self._visible(provider_id, attempted_action="llm_provider.refresh")
        if provider is None:
            return None
        return await self._run_discovery(provider)

    async def delete(self, provider_id: UUID) -> bool:
        """Delete a provider + its stored API key, else 404.

        Tenant visibility is established before any write. In one transaction: the
        CC-C vault secret (if any) is deleted, then the provider row. Audits
        ``llm_provider.deleted`` (INV-6). Returns ``False`` when not visible.
        """
        await self._require_admin(
            attempted_action="llm_provider.delete", resource_id=str(provider_id)
        )
        provider = await self._visible(provider_id, attempted_action="llm_provider.delete")
        if provider is None:
            return False

        if provider.api_key_secret_ref is not None:
            await self._delete_api_key_secret(provider.api_key_secret_ref)

        deleted = await self._providers.delete(provider_id)
        if not deleted:  # pragma: no cover — visibility already established
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


def _safe_error(exc: Exception) -> str:
    """A short, safe failure reason for ``last_error`` (never the credential).

    Uses the exception type + message, truncated — the API key is never in an
    egress/httpx/JSON error, and the resolver's own errors carry only the ref, so
    this is safe to persist and surface. Bounded to :data:`_MAX_ERROR_LEN`.
    """
    detail = str(exc).strip() or exc.__class__.__name__
    message = f"{exc.__class__.__name__}: {detail}"
    if len(message) > _MAX_ERROR_LEN:
        return message[: _MAX_ERROR_LEN - 1] + "…"
    return message


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
    "build_llm_provider_service",
]
