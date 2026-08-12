"""MCP server registration use-cases — the ``/mcp-servers`` surface (#226, ADR-0012 §5).

The orchestration layer for per-tenant MCP server registration (ADR-0004:
``services/`` compose adapters; routers call exactly one service). It pairs:

* the tenant-scoped ``db/`` :class:`~app.db.repositories.McpServerRepository` — the
  only SQL for ``mcp_servers``;
* the CC-C secrets vault (:class:`~app.services.secrets_service.SecretsService`,
  issue #209) — the auth credential is stored **write-only** (``store_secret``
  returns a ref + masked hint, never the value) and resolved in-process at connect
  time (``get_secret_plaintext``); the row keeps only ``auth_secret_ref`` +
  ``secret_hint``;
* the merged MCP adapter (:mod:`app.mcp`, issue #225) — ``health`` + ``list_tools``
  through the SSRF-guarded egress; the per-tenant MCP rate limit is bound to the
  Redis limiter for the adapter's egress seam (ADR-0012 §4);
* the one ``AuditSink`` (spec 0004 §2.4) — ``mcp_server.registered`` / ``updated``
  / ``deleted`` / ``tested`` (ADR-0012 §5, INV-6).

**Tenancy + ownership (spec 0004 §2.1/§2.2, INV-1/INV-2 — deny by default).**
Every operation is scoped to the caller's tenant (the repository) *and* the
caller's ownership (this service — owner **or** tenant admin). A server in another
tenant, or owned by another user (and the caller is not an admin), is treated as
**non-existent**: the read/op returns ``None`` and the router maps that to
**404** (existence non-disclosure; never 403). ``owner_id``/``tenant_id`` come
from the resolved principal, never from request input.

**SSRF (ADR-0012 §4, load-bearing).** ``register``/``update`` validate the
endpoint before a row is written: https-only + the shared range check
(:mod:`app.net.egress`) — a non-https, unresolvable, or blocked (loopback /
private / link-local / CGNAT / cloud-metadata) endpoint raises a typed
``ValidationError`` (``endpoint_blocked`` / ``endpoint_scheme``) → **422**. An
unsupported / ``stdio`` transport is ``unsupported_transport`` → **422**. The full
per-hop guard runs again inside the adapter at connect time.

**The credential is never returned.** No method or wire shape here exposes the
stored secret value — only the masked ``secret_hint`` (CC-C AC-3). The plaintext
is reachable exclusively through the in-process ``SecretsService.get_secret_plaintext``
the adapter's auth resolver calls at connect time.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.db.repositories import McpServerRepository
from app.domain.audit import AuditAction, AuditActor
from app.domain.entities import (
    AuditOutcome,
    McpServer,
    McpServerStatus,
    Role,
    SecretKind,
)
from app.mcp import (
    AuthResolver,
    McpClient,
    McpHealth,
    McpServerConfig,
    McpToolResult,
    McpToolSpec,
    McpTransport,
)
from app.mcp.client import RateLimitCheck
from app.net.egress import EgressBlockedError, resolve_safe_ip
from app.services.audit import AuditSink, PermissionDeniedContext
from app.services.secrets_service import SecretsService, build_secrets_service
from app.services.tools.mcp_bridge import tools_for_servers
from app.services.tools.types import ToolDefinition
from app.tasks.rate_limit import AsyncRateLimiter, RedisFixedWindowRateLimiter

log = get_logger(__name__)

# Pagination bounds mirror the contract's Limit parameter (min 1, max 100).
_MIN_LIMIT = 1
_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

# A short, stable cursor prefix so a decoded payload is recognisably one of ours.
_CURSOR_PREFIX = "mcp:"

# MCP servers carry credentials, so — like the adapter's egress guard — only https
# is accepted for a remote endpoint (ADR-0012 §4, stricter than the web connector).
_ALLOWED_SCHEME = "https"

# The stable name under which an MCP server's auth credential is stored in the CC-C
# vault (per-owner singleton keyed on this + the server id). Kept a fixed prefix so
# a rotation re-stores in place and a delete can find it.
_SECRET_NAME_PREFIX = "mcp-auth"

# The Redis keyspace the per-tenant MCP egress limiter counts in (ADR-0012 §4) —
# distinct from the connector-sync / web-search / run limiter namespaces so the
# windows never collide.
_MCP_RATE_KEY_PREFIX = "lumen:ratelimit:mcp"


@dataclass(frozen=True, slots=True)
class McpAuthInput:
    """The write-only auth material accepted on register/rotate (contract ``McpServerAuth``).

    ``kind`` is ``bearer`` (an Authorization bearer token) or ``header`` (the value
    set as ``header_name``); ``value`` is the secret (write-only — never echoed).
    Flattened into a single plaintext the CC-C vault stores; the adapter attaches it
    at connect time (the #225 adapter attaches a bearer header — a custom header is
    persisted for a future adapter hop, its value never returned either way).
    """

    kind: str
    value: str
    header_name: str | None = None


@dataclass(frozen=True, slots=True)
class McpServerPage:
    """One page of MCP servers plus the opaque cursor for the next page."""

    items: list[McpServer]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class McpToolView:
    """One discovered tool projected for the ``/mcp-servers/{id}/tools`` surface.

    ``name`` is the namespaced registry name (``mcp:<slug>:<raw>``, ADR-0012 §6);
    ``raw_name`` the server's own name; ``risk_tier`` is T0 for a server-annotated
    read-only tool, else the default T2 (trust is earned). The registry *injection*
    is #227 — this view just surfaces the last discovery snapshot with the tier the
    contract exposes.
    """

    name: str
    raw_name: str
    description: str | None
    input_schema: dict[str, object]
    risk_tier: str
    read_only: bool


def _encode_cursor(server_id: UUID) -> str:
    """Encode a boundary server id as an opaque URL-safe cursor."""
    raw = f"{_CURSOR_PREFIX}{server_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> UUID:
    """Decode an opaque cursor back into the boundary server id (fail-closed 422)."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc
    if not raw.startswith(_CURSOR_PREFIX):
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor")
    try:
        return UUID(raw[len(_CURSOR_PREFIX) :])
    except ValueError as exc:
        raise ValidationError("Invalid pagination cursor.", code="invalid_cursor") from exc


def _clamp_limit(limit: int | None) -> int:
    """Clamp the requested page size into the contract's [1, 100] band."""
    if limit is None:
        return _DEFAULT_LIMIT
    return max(_MIN_LIMIT, min(_MAX_LIMIT, limit))


def _slug_for(server_id: UUID) -> str:
    """A stable, log-safe slug for a server (used in adapter logs + tool namespacing).

    Derived from the server id so it is unique per server and never carries a
    credential or a user-controlled string into structured logs.
    """
    return f"srv-{server_id.hex[:12]}"


def _namespaced_tool_name(slug: str, raw_name: str) -> str:
    """The ``mcp:<slug>:<tool>`` registry name for a discovered tool (ADR-0012 §6)."""
    return f"mcp:{slug}:{raw_name}"


def _tool_snapshot(tools: list[McpToolSpec]) -> list[dict[str, object]]:
    """Project the adapter's tool specs into the portable ``discovered_tools`` JSON.

    Stores the server-advertised ``name``, ``description``, ``input_schema``, and
    the ``read_only`` annotation — never a credential (the adapter never puts one in
    a spec). #227 injects these into the CC-A registry namespaced; this snapshot is
    what ``/mcp-servers/{id}/tools`` reads back.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
            "read_only": spec.read_only,
        }
        for spec in tools
    ]


def _flatten_auth(auth: McpAuthInput) -> str:
    """Flatten the write-only auth material into the single plaintext the vault stores.

    A ``bearer`` credential stores the bare token; a ``header`` credential stores
    ``<header_name>: <value>`` so a future adapter hop can reconstruct the header.
    Either way the value is envelope-encrypted by CC-C and never returned.
    """
    if auth.kind == "header":
        return f"{auth.header_name}: {auth.value}"
    return auth.value


class McpServersService:
    """Register / test / list / update / delete MCP servers for one principal (#226).

    Constructed per-request with the session, the resolved ``tenant_id`` +
    ``owner_id`` + the caller's ``roles`` (owner-or-admin), the CC-C
    :class:`SecretsService`, an :class:`McpClient` factory (production wires the real
    adapter; a test injects one over an in-process fixture transport), the per-tenant
    :class:`RateLimiter`, the outbound ``user_agent``, and the audit sink +
    correlation context. All ownership/tenancy enforcement lives here; the router
    only (de)serialises and maps ``None`` → 404.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        owner_id: UUID,
        roles: tuple[Role, ...],
        secrets: SecretsService,
        client_factory: McpClientFactory,
        rate_limiter: AsyncRateLimiter,
        user_agent: str,
        audit: AuditSink,
        denials: PermissionDeniedContext | None,
        request_id: str,
        source_ip: str,
        allowed_transports: frozenset[McpTransport] | None = None,
        endpoint_allowlist: frozenset[str] = frozenset(),
        connect_timeout_seconds: float = 15.0,
        call_timeout_seconds: float = 30.0,
    ) -> None:
        self._session = session
        self._servers = McpServerRepository(session, tenant_id)
        self._tenant_id = tenant_id
        self._owner_id = owner_id
        self._is_admin = Role.ADMIN in roles
        self._secrets = secrets
        self._client_factory = client_factory
        self._rate_limiter = rate_limiter
        self._user_agent = user_agent
        self._audit = audit
        self._denials = denials
        if self._denials is not None:
            self._denials.assert_user(owner_id)
        self._request_id = request_id
        self._source_ip = source_ip
        self._allowed_transports = (
            allowed_transports if allowed_transports is not None else frozenset(McpTransport)
        )
        self._endpoint_allowlist = endpoint_allowlist
        self._connect_timeout_seconds = connect_timeout_seconds
        self._call_timeout_seconds = call_timeout_seconds

    # --- internal helpers ---------------------------------------------------

    def _may_access(self, server: McpServer) -> bool:
        """Deny-by-default ownership check (spec 0004 §2.2, owner-or-admin)."""
        return self._is_admin or server.owner_id == self._owner_id

    async def _visible(self, server_id: UUID, *, attempted_action: str) -> McpServer | None:
        """Fetch a server the caller may see, or ``None`` (→ 404).

        ``None`` for a missing id, a foreign-tenant id (the repository sees no row),
        *or* a same-tenant server owned by another user the caller does not admin —
        INV-1/INV-2 collapse all three to 404 at the router.
        """
        server = await self._servers.get(server_id)
        if server is None or not self._may_access(server):
            if self._denials is None:
                raise RuntimeError("MCP direct-resource guards require a denial context.")
            await self._denials.emit(
                resource_type="mcp_server",
                resource_id=str(server_id),
                attempted_action=attempted_action,
                reason="not_visible",
            )
            return None
        return server

    def _parse_transport(self, transport: str) -> McpTransport:
        """Resolve a wire transport string to the domain enum, or 422 (ADR-0012 §1).

        An unknown value — including the deferred ``stdio``/local-process transport
        — is ``unsupported_transport`` (422). A configured-but-disabled remote
        transport is likewise refused (deny by default).
        """
        try:
            parsed = McpTransport(transport)
        except ValueError as exc:
            raise ValidationError(
                f"unsupported MCP transport {transport!r} "
                "(only streamable_http / sse ship in v1; stdio is deferred)",
                code="unsupported_transport",
            ) from exc
        if parsed not in self._allowed_transports:
            raise ValidationError(
                f"MCP transport {transport!r} is not enabled",
                code="unsupported_transport",
            )
        return parsed

    def _validate_endpoint(self, endpoint_url: str) -> None:
        """https-only + SSRF pre-check before a row is written (ADR-0012 §4).

        The register/connect chokepoint's synchronous half: refuse a non-https
        scheme or a missing host, then resolve-all/reject-any via the shared egress
        predicate (loopback / private / link-local / CGNAT / cloud-metadata). A
        rejection is a 422 whose ``code`` distinguishes the case
        (``endpoint_scheme`` / ``endpoint_blocked``, INV-8). The adapter re-runs the
        full per-hop guard (incl. redirects + IP-pin) at connect time — this is the
        *pre-write* gate, not the only one.
        """
        parts = urlsplit(endpoint_url)
        scheme = parts.scheme.lower()
        if scheme != _ALLOWED_SCHEME:
            raise ValidationError(
                f"MCP endpoint scheme {scheme or '(none)'!r} is not allowed; "
                "only https is permitted (credentials in transit)",
                code="endpoint_scheme",
            )
        host = parts.hostname
        if not host:
            raise ValidationError("MCP endpoint URL has no host.", code="endpoint_scheme")
        if self._endpoint_allowlist and host not in self._endpoint_allowlist:
            raise ValidationError(
                f"host {host!r} is not on the MCP endpoint allowlist.",
                code="endpoint_blocked",
            )
        try:
            resolve_safe_ip(host)
        except EgressBlockedError as exc:
            raise ValidationError(str(exc), code="endpoint_blocked") from exc

    def _config_for(self, server: McpServer) -> McpServerConfig:
        """Build the adapter's connection config from a persisted row (no credential).

        The ``auth_secret_ref`` is passed as an opaque handle; the adapter resolves
        it to a plaintext bearer token in-process at connect time via the auth
        resolver (:meth:`_resolve_auth`), never here and never on the config.
        """
        return McpServerConfig(
            slug=_slug_for(server.id),
            endpoint_url=server.endpoint_url,
            transport=McpTransport(server.transport),
            auth_secret_ref=server.auth_secret_ref,
        )

    async def _resolve_auth(self, auth_secret_ref: str) -> str | None:
        """Resolve a CC-C secret ref to its plaintext (in-process, never returned/logged).

        The adapter's :data:`~app.mcp.types.AuthResolver` seam: given a server's
        ``auth_secret_ref``, decrypt the credential from the vault at connect time
        (owner-or-admin gated inside the secrets service; a ``secret.accessed``
        audit records the read, never the value). Returns ``None`` for a missing /
        malformed ref (an anonymous connect) rather than raising into the adapter.
        """
        try:
            secret_id = UUID(auth_secret_ref)
        except ValueError:
            return None
        try:
            return await self._secrets.get_secret_plaintext(secret_id, accessor=AuditActor.system())
        except Exception:  # noqa: BLE001 — a resolver failure must not crash the probe
            log.warning("mcp_server.auth_resolve_failed", ref=auth_secret_ref)
            return None

    def _build_client(self) -> McpClient:
        """Assemble an :class:`McpClient` bound to this tenant's rate limit + auth.

        The rate-limit seam is bound to the Redis fixed-window limiter keyed on the
        tenant (``RateLimiter.try_acquire``), so a tenant cannot fan out unbounded
        MCP egress (ADR-0012 §4). The auth resolver is the in-process CC-C decrypt.
        The SSRF guard inside the adapter remains the authoritative safety control.
        """
        return self._client_factory(
            McpClientParams(
                auth_resolver=self._resolve_auth,
                connect_timeout_seconds=self._connect_timeout_seconds,
                call_timeout_seconds=self._call_timeout_seconds,
                user_agent=self._user_agent,
                allowed_transports=self._allowed_transports,
                endpoint_allowlist=self._endpoint_allowlist,
                rate_limit=self._tenant_rate_limit,
            )
        )

    async def _tenant_rate_limit(self) -> bool:
        """The adapter's zero-arg rate-limit predicate, bound to this tenant's window.

        Awaitable because it is evaluated inside ``McpClient.connect`` on the
        serving event loop; the blocking form parked the worker on a fresh
        Redis connect for every MCP call (#527).
        """
        return await self._rate_limiter.try_acquire_async(self._tenant_id)

    async def _store_auth(self, server_id: UUID, auth: McpAuthInput) -> tuple[UUID, str]:
        """Store the write-only credential via CC-C; return ``(secret_id, hint)``.

        The value is envelope-encrypted in the vault under a per-server singleton
        name (so a rotation re-stores in place); ``store_secret`` returns a
        plaintext-free ref carrying only the masked hint. The value never touches
        this row, this log, or any response.
        """
        ref = await self._secrets.store_secret(
            name=f"{_SECRET_NAME_PREFIX}:{server_id}",
            kind=SecretKind.MCP_AUTH,
            plaintext=_flatten_auth(auth),
        )
        return ref.id, ref.hint

    # --- use-cases ----------------------------------------------------------

    async def register(
        self,
        *,
        name: str,
        transport: str,
        endpoint_url: str,
        auth: McpAuthInput | None,
    ) -> McpServer:
        """Register a remote MCP server (ADR-0012 §5); return it ``status=pending``.

        Order (fail-closed):

        1. **Transport** — resolve/validate (unsupported/``stdio`` → 422
           ``unsupported_transport``, ADR-0012 §1).
        2. **Endpoint** — https + SSRF pre-check *before* anything is written
           (non-https → 422 ``endpoint_scheme``; blocked → 422 ``endpoint_blocked``,
           ADR-0012 §4).
        3. **Row** — create ``status=pending`` (no first probe on the request path;
           the caller runs ``POST .../test`` to probe + discover).
        4. **Credential** — if supplied, store it write-only via CC-C and attach the
           ``auth_secret_ref`` + masked ``secret_hint`` to the row (never the value).
        5. **Audit** ``mcp_server.registered`` (INV-6) — metadata + hint, never the
           value.

        Raises:
            ValidationError: unsupported transport, or an invalid / non-https /
                SSRF-blocked endpoint — all mapped to **422** (INV-8), the ``code``
                distinguishing the case.
        """
        parsed = self._parse_transport(transport)
        self._validate_endpoint(endpoint_url)

        server = await self._servers.create(
            owner_id=self._owner_id,
            name=name,
            transport=parsed.value,
            endpoint_url=endpoint_url,
            auth_secret_ref=None,
            secret_hint=None,
        )

        if auth is not None:
            secret_id, hint = await self._store_auth(server.id, auth)
            updated = await self._servers.update(
                server.id, auth_secret_ref=secret_id, secret_hint=hint
            )
            assert updated is not None  # just created  # noqa: S101
            server = updated

        await self._audit.emit(
            action=AuditAction.MCP_SERVER_REGISTERED,
            actor=AuditActor.user(self._owner_id),
            resource_type="mcp_server",
            resource_id=str(server.id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "name": server.name,
                "transport": server.transport,
                "has_auth": server.auth_secret_ref is not None,
                "secret_hint": server.secret_hint,
            },
        )
        return server

    async def list_page(self, *, cursor: str | None, limit: int | None) -> McpServerPage:
        """Return one keyset page of the caller's own MCP servers (newest first).

        Owner- and tenant-scoped (deny by default). Fetches ``limit + 1`` rows to
        decide whether a next page exists; the extra row (if present) sets
        ``next_cursor`` and is then dropped.
        """
        page_size = _clamp_limit(limit)
        after_id = _decode_cursor(cursor) if cursor else None
        rows = await self._servers.list_for_owner_page(
            self._owner_id, limit=page_size + 1, after_id=after_id
        )
        has_more = len(rows) > page_size
        page = rows[:page_size]
        next_cursor = _encode_cursor(page[-1].id) if has_more and page else None
        return McpServerPage(items=page, next_cursor=next_cursor)

    async def get(self, server_id: UUID) -> McpServer | None:
        """Fetch one of the caller's servers, or ``None`` if not visible (→ 404)."""
        return await self._visible(server_id, attempted_action="mcp_server.read")

    async def update(
        self,
        server_id: UUID,
        *,
        name: str | None = None,
        transport: str | None = None,
        endpoint_url: str | None = None,
        enabled: bool | None = None,
        auth: McpAuthInput | None = None,
        clear_auth: bool = False,
    ) -> McpServer | None:
        """Update a registered server (rename, toggle, rotate/clear credential), or None.

        Visibility (tenant + owner-or-admin) is established before any write.
        Changing ``endpoint_url`` re-runs the https + SSRF validation (else 422); a
        supplied ``transport`` is re-validated (unsupported/``stdio`` → 422). Rotating
        ``auth`` stores the new value write-only via CC-C and replaces the ref +
        hint; ``clear_auth`` deletes the vault secret and nulls the ref + hint. The
        credential value is never returned — only the updated masked ``secret_hint``.
        Audits ``mcp_server.updated`` (INV-6). ``None`` when not visible (→ 404).
        """
        server = await self._visible(server_id, attempted_action="mcp_server.update")
        if server is None:
            return None

        if transport is not None:
            self._parse_transport(transport)  # validate only (transport is immutable-shaped)
        if endpoint_url is not None:
            self._validate_endpoint(endpoint_url)

        new_ref: UUID | None = None
        new_hint: str | None = None
        do_clear = False
        if clear_auth:
            do_clear = True
            if server.auth_secret_ref is not None:
                await self._delete_auth_secret(server.auth_secret_ref)
        elif auth is not None:
            new_ref, new_hint = await self._store_auth(server.id, auth)

        updated = await self._servers.update(
            server_id,
            name=name,
            endpoint_url=endpoint_url,
            enabled=enabled,
            auth_secret_ref=new_ref,
            secret_hint=new_hint,
            clear_auth=do_clear,
        )
        if updated is None:  # pragma: no cover — visibility already established
            return None

        await self._audit.emit(
            action=AuditAction.MCP_SERVER_UPDATED,
            actor=AuditActor.user(self._owner_id),
            resource_type="mcp_server",
            resource_id=str(server_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "name": updated.name,
                "transport": updated.transport,
                "has_auth": updated.auth_secret_ref is not None,
                "secret_hint": updated.secret_hint,
            },
        )
        return updated

    async def delete(self, server_id: UUID) -> bool:
        """Delete a server + its stored credential + discovered tools, else 404.

        Visibility (tenant + owner-or-admin) is established before any write so a
        non-owner's server is never touched and is reported as 404 (INV-1/INV-2). In
        one transaction: the CC-C vault secret (if any) is deleted, then the server
        row. Audits ``mcp_server.deleted`` (INV-6). Returns ``False`` when not
        visible.
        """
        server = await self._visible(server_id, attempted_action="mcp_server.delete")
        if server is None:
            return False

        if server.auth_secret_ref is not None:
            await self._delete_auth_secret(server.auth_secret_ref)

        deleted = await self._servers.delete(server_id)
        if not deleted:  # pragma: no cover — visibility already established
            return False

        await self._audit.emit(
            action=AuditAction.MCP_SERVER_DELETED,
            actor=AuditActor.user(self._owner_id),
            resource_type="mcp_server",
            resource_id=str(server_id),
            outcome=AuditOutcome.ALLOWED,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={"name": server.name, "transport": server.transport},
        )
        return True

    async def test(self, server_id: UUID) -> McpServer | None:
        """Probe health + re-discover tools; persist the outcome, or 404.

        Runs a bounded liveness/handshake probe through the SSRF-guarded adapter
        (``health`` + ``list_tools``, ADR-0012 §2/§4). On success ``status`` becomes
        ``ready``, ``last_health_at`` is stamped, ``last_error`` cleared, and the
        discovered-tool snapshot refreshed; a down / erroring / slow server marks
        ``status=error`` with a safe ``last_error`` (the previous tool snapshot is
        preserved) — the probe **never** throws (ADR-0012 §7). The credential is
        fetched in-process from CC-C at connect time and never returned. Audits
        ``mcp_server.tested`` (INV-6). ``None`` when not visible (→ 404).
        """
        server = await self._visible(server_id, attempted_action="mcp_server.test")
        if server is None:
            return None

        client = self._build_client()
        config = self._config_for(server)
        health: McpHealth = await client.health(config)

        if health.ok:
            tools = await client.list_tools(config)
            updated = await self._servers.update_health(
                server_id,
                status=McpServerStatus.READY,
                last_health_at=_utc_now(),
                last_error=None,
                discovered_tools=_tool_snapshot(tools),
            )
        else:
            updated = await self._servers.update_health(
                server_id,
                status=McpServerStatus.ERROR,
                last_health_at=server.last_health_at,
                last_error=health.detail or "the MCP server is unavailable",
                discovered_tools=server.discovered_tools,
            )
        assert updated is not None  # visibility already established  # noqa: S101

        await self._audit.emit(
            action=AuditAction.MCP_SERVER_TESTED,
            actor=AuditActor.user(self._owner_id),
            resource_type="mcp_server",
            resource_id=str(server_id),
            outcome=AuditOutcome.ALLOWED if health.ok else AuditOutcome.ERROR,
            request_id=self._request_id,
            source_ip=self._source_ip,
            metadata={
                "status": updated.status.value,
                "tool_count": len(updated.discovered_tools),
            },
        )
        return updated

    async def resolve_run_tools(self) -> dict[str, ToolDefinition]:
        """The caller's registered+enabled MCP tools as governed CC-A definitions (#227).

        The per-run resolver the chat runtime uses (never a global registration —
        MCP tools are tenant-scoped and dynamic, so resolving them here per run keeps
        INV-1/INV-2: only *this* caller's own, enabled servers in *this* tenant
        contribute, and a disabled / foreign server never loads). Each discovered
        tool becomes a namespaced ``mcp:<slug>:<tool>`` :class:`ToolDefinition` whose
        handler invokes the tool through this service's SSRF-guarded, rate-limited,
        auth-resolving :class:`McpClient` (auth fetched from CC-C at call time). The
        returned map is handed to the runner as its ``extra_tools`` for that answer;
        the runner's allow-list is still the hard chokepoint on which of them are
        actually offered/invokable.
        """
        servers = await self._servers.list_enabled_for_owner(self._owner_id)
        if not servers:
            return {}
        client = self._build_client()

        async def _invoke(
            config: McpServerConfig, name: str, args: dict[str, object]
        ) -> McpToolResult:
            return await client.call_tool(config, name, args)

        return tools_for_servers(servers, _invoke)

    async def list_tools(self, server_id: UUID) -> list[McpToolView] | None:
        """The tools discovered on the last successful probe (ADR-0012 §6), or None.

        Reads the persisted ``discovered_tools`` snapshot (no live connect) and
        projects each to its namespaced registry name + inferred risk tier — T0 for
        a server-annotated read-only tool, else the default T2 (trust is earned).
        ``None`` when the server is not visible (→ 404).
        """
        server = await self._visible(server_id, attempted_action="mcp_server.tools.read")
        if server is None:
            return None
        slug = _slug_for(server.id)
        views: list[McpToolView] = []
        for raw in server.discovered_tools:
            raw_name = str(raw.get("name", ""))
            read_only = bool(raw.get("read_only", False))
            schema = raw.get("input_schema")
            views.append(
                McpToolView(
                    name=_namespaced_tool_name(slug, raw_name),
                    raw_name=raw_name,
                    description=(
                        str(raw["description"]) if raw.get("description") is not None else None
                    ),
                    input_schema=schema if isinstance(schema, dict) else {},
                    risk_tier="T0" if read_only else "T2",
                    read_only=read_only,
                )
            )
        return views

    async def _delete_auth_secret(self, auth_secret_ref: str) -> None:
        """Best-effort delete of the CC-C vault secret backing a server's credential.

        A rotation/clear/delete removes the old secret so the vault does not
        accumulate orphans. A missing/foreign secret is a 404 inside the vault
        (idempotent from our view) — swallowed so a stale ref never blocks the
        server mutation.
        """
        try:
            await self._secrets.delete_secret(UUID(auth_secret_ref))
        except Exception:  # noqa: BLE001 — a stale/foreign ref must not block the mutation
            log.warning("mcp_server.auth_secret_delete_failed", ref=auth_secret_ref)


def _utc_now() -> datetime:
    """Current UTC timestamp (isolated so tests read one clock)."""
    return datetime.now(UTC)


# --- Adapter client factory seam --------------------------------------------
#
# The service builds an ``McpClient`` per test-probe from these params. Production
# wires ``default_mcp_client_factory`` (a real network transport); a test injects a
# factory that passes the fixture's in-process ``inner_transport`` so the full
# connect/list/health round-trip runs offline through the REAL SSRF guard + SDK.


@dataclass(frozen=True, slots=True)
class McpClientParams:
    """The knobs a factory needs to build one :class:`McpClient` (rate-limit bound)."""

    auth_resolver: AuthResolver
    connect_timeout_seconds: float
    call_timeout_seconds: float
    user_agent: str
    allowed_transports: frozenset[McpTransport]
    endpoint_allowlist: frozenset[str]
    rate_limit: RateLimitCheck


class McpClientFactory(Protocol):
    """Builds an :class:`McpClient` from :class:`McpClientParams` (the injectable seam)."""

    def __call__(self, params: McpClientParams) -> McpClient: ...


def default_mcp_client_factory(params: McpClientParams) -> McpClient:
    """Production factory: a real-network :class:`McpClient` (no injected transport)."""
    return McpClient(
        auth_resolver=params.auth_resolver,
        connect_timeout_seconds=params.connect_timeout_seconds,
        call_timeout_seconds=params.call_timeout_seconds,
        user_agent=params.user_agent,
        allowed_transports=params.allowed_transports,
        endpoint_allowlist=params.endpoint_allowlist,
        rate_limit=params.rate_limit,
    )


def build_transport_client_factory(
    inner_transport: httpx.AsyncBaseTransport,
) -> McpClientFactory:
    """A factory that pins the adapter to an in-process ``inner_transport`` (tests).

    The SSRF guard still wraps the injected transport, so a test drives the full
    real round-trip against an ASGI fixture MCP server with no socket (mirrors the
    #225 adapter tests). Production never uses this.
    """

    def _factory(params: McpClientParams) -> McpClient:
        return McpClient(
            auth_resolver=params.auth_resolver,
            connect_timeout_seconds=params.connect_timeout_seconds,
            call_timeout_seconds=params.call_timeout_seconds,
            user_agent=params.user_agent,
            allowed_transports=params.allowed_transports,
            endpoint_allowlist=params.endpoint_allowlist,
            rate_limit=params.rate_limit,
            inner_transport=inner_transport,
        )

    return _factory


def build_mcp_servers_service(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: UUID,
    owner_id: UUID,
    roles: tuple[Role, ...],
    audit: AuditSink,
    denials: PermissionDeniedContext | None,
    request_id: str,
    source_ip: str,
    client_factory: McpClientFactory | None = None,
    rate_limiter: AsyncRateLimiter | None = None,
) -> McpServersService:
    """Assemble a :class:`McpServersService` from settings (the production wiring).

    Config is the single env reader (``core/config.py``); the caller passes a
    ``Settings`` rather than reading the environment. This factory builds the CC-C
    :class:`SecretsService` (via :func:`build_secrets_service`, the sole cipher
    importer — so no ``api/`` module ever touches the cipher/plaintext) and the
    per-tenant MCP egress rate limiter (the Redis fixed-window limiter under the MCP
    keyspace, ADR-0012 §4). It uses the real client factory (a real network
    transport) unless one is injected (a test passes a fixture-bound factory); a
    test may likewise inject a fake ``rate_limiter``. The allowed transports /
    endpoint allowlist / timeouts all come from the MCP config block (ADR-0012
    §1/§4).
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
    limiter = rate_limiter or RedisFixedWindowRateLimiter(
        settings.redis_url,
        max_per_window=settings.mcp_rate_max_per_window,
        window_seconds=settings.mcp_rate_window_seconds,
        key_prefix=_MCP_RATE_KEY_PREFIX,
    )
    return McpServersService(
        session,
        tenant_id=tenant_id,
        owner_id=owner_id,
        roles=roles,
        secrets=secrets,
        client_factory=client_factory or default_mcp_client_factory,
        rate_limiter=limiter,
        user_agent=settings.web_user_agent,
        audit=audit,
        denials=denials,
        request_id=request_id,
        source_ip=source_ip,
        allowed_transports=frozenset(McpTransport(t) for t in settings.mcp_allowed_transports),
        endpoint_allowlist=settings.mcp_endpoint_allowlist,
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
        call_timeout_seconds=settings.mcp_call_timeout_seconds,
    )


__all__ = [
    "McpAuthInput",
    "McpClientFactory",
    "McpClientParams",
    "McpServerPage",
    "McpServersService",
    "McpToolView",
    "build_mcp_servers_service",
    "build_transport_client_factory",
    "default_mcp_client_factory",
]
