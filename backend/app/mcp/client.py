"""The MCP client adapter — the ONLY importer of the MCP SDK (ADR-0012 §2).

This module owns every call to the official MCP Python SDK and exposes **domain
types only** (``app.mcp.types``) upward: no SDK object, JSON-RPC frame, or MCP
schema type crosses the boundary. It provides the four operations #226/#227 build
on:

* :meth:`McpClient.connect` — open + handshake/capability-negotiate a transport
  (Streamable HTTP or SSE) to a server, through the SSRF egress guard (§4). Auth
  is resolved from CC-C **at connect time** and attached to the transport;
* :meth:`McpClient.list_tools` — discover the server's tools, normalized to
  :class:`~app.mcp.types.McpToolSpec`;
* :meth:`McpClient.call_tool` — invoke one tool and return a typed
  :class:`~app.mcp.types.McpToolResult`;
* :meth:`McpClient.health` — a bounded liveness/handshake probe.

**Failure isolation (ADR-0012 §7).** Every failure mode — a blocked egress
target, a down/refusing server, a timeout, a protocol fault, a malformed frame —
is caught and returned as a typed result (``ok=False`` / an ``error`` health),
**never** an exception escaping the module. The chat run reads the result and
continues.

**Secret hygiene (ADR-0012 §3, CC-C #209).** The auth token is fetched in-process
from the injected resolver at connect time, attached to the transport as a bearer
header, and **never** logged, never placed in a result, never serialized. The
structured logs key only off the log-safe server ``slug``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from functools import partial
from types import TracebackType
from typing import Any

import httpx
import structlog

from app.mcp.egress import McpEgressBlockedError, build_guarded_client
from app.mcp.types import (
    MCP_ERROR_EGRESS_BLOCKED,
    MCP_ERROR_RATE_LIMITED,
    MCP_ERROR_TIMEOUT,
    MCP_ERROR_TOOL_ERROR,
    MCP_ERROR_UNAVAILABLE,
    AuthResolver,
    McpHealth,
    McpHealthState,
    McpServerConfig,
    McpToolResult,
    McpToolSpec,
    McpTransport,
)
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent, Tool

_log = structlog.get_logger(__name__)

# The per-tenant rate-limit seam: a zero-arg predicate (True = admit, False =
# throttle). #226 binds it to the Redis fixed-window limiter keyed on the tenant
# (``RateLimiter.try_acquire`` with the MCP keyspace + window, ADR-0012 §4); tests
# pass a lambda. Kept a bare ``Callable`` so the adapter needs no Redis import and
# the SSRF guard stays the authoritative safety control if none is wired.
RateLimitCheck = Callable[[], bool]


class McpSession:
    """A live, handshaken MCP session — a domain handle, no SDK type exposed.

    Returned by :meth:`McpClient.connect` as an async context manager. It owns the
    transport + :class:`ClientSession` lifetimes via an :class:`AsyncExitStack`;
    the underlying SDK session is private. ``list_tools`` / ``call_tool`` operate
    on the open session and return domain types. Closing it tears down the
    transport (and its guarded httpx client) cleanly.
    """

    def __init__(
        self,
        *,
        slug: str,
        session: ClientSession,
        exit_stack: AsyncExitStack,
        call_timeout_seconds: float,
    ) -> None:
        self._slug = slug
        self._session = session
        self._exit_stack = exit_stack
        self._call_timeout_seconds = call_timeout_seconds

    async def list_tools(self) -> list[McpToolSpec]:
        """Discover the server's tools, normalized to :class:`McpToolSpec`.

        Never raises out of the module: a protocol/transport fault yields an empty
        list (the failure surfaces via :meth:`McpClient.health` / the connect
        path). Read-only annotation is honoured (``readOnlyHint``); unannotated ⇒
        ``read_only=False`` (trust is earned — #227 defaults it to T2).
        """
        try:
            result = await asyncio.wait_for(
                self._session.list_tools(), timeout=self._call_timeout_seconds
            )
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - contain everything
            _log.warning("mcp.list_tools_failed", slug=self._slug, error=type(exc).__name__)
            return []
        return [_to_tool_spec(tool) for tool in result.tools]

    async def call_tool(self, name: str, args: dict[str, Any]) -> McpToolResult:
        """Invoke tool ``name`` with ``args``; return a typed contained result.

        Bounds the call with the per-call timeout. Any failure — server down,
        timeout, protocol fault, malformed reply, or an application-level tool
        error (MCP ``isError``) — is a typed ``ok=False`` result, never a raise
        (ADR-0012 §7). The arguments are NOT logged (they may carry sensitive
        values); only the tool name + a boolean outcome are.
        """
        try:
            result: CallToolResult = await asyncio.wait_for(
                self._session.call_tool(name, arguments=args),
                timeout=self._call_timeout_seconds,
            )
        except TimeoutError:
            _log.warning("mcp.call_tool_timeout", slug=self._slug, tool=name)
            return McpToolResult.failure(
                error_code=MCP_ERROR_TIMEOUT,
                content=f"the MCP tool {name!r} timed out",
            )
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - contain everything
            _log.warning(
                "mcp.call_tool_failed", slug=self._slug, tool=name, error=type(exc).__name__
            )
            return McpToolResult.failure(
                error_code=MCP_ERROR_UNAVAILABLE,
                content=f"the MCP tool {name!r} could not be invoked",
            )
        return _to_tool_result(name, result)

    async def aclose(self) -> None:
        """Tear down the session + transport (and its guarded httpx client)."""
        await self._exit_stack.aclose()

    async def __aenter__(self) -> McpSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class McpConnectError(Exception):
    """Raised **inside** the module when connect fails; never escapes upward.

    :meth:`McpClient.connect` catches it and re-raises the typed
    :class:`McpConnectionFailed` with an ``error_code`` the caller can map, so the
    public surface is a single, contained failure type carrying a stable code.
    """

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


class McpConnectionFailed(Exception):
    """The public, contained connect failure — carries a stable ``error_code``.

    :meth:`McpClient.connect` raises exactly this on any failure (egress blocked,
    server down, timeout, protocol fault). A caller (#226/#227) turns it into an
    ``ok=False`` tool result / an ``error`` health without ever seeing an SDK
    exception. It is the *only* exception ``connect`` raises.
    """

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


class McpClient:
    """The MCP client adapter (ADR-0012 §2) — connect / list / call / health.

    Constructed with the resolved config knobs and the CC-C auth resolver +
    (optional) per-tenant rate limiter. Holds no per-server state; each
    :meth:`connect` opens a fresh guarded transport. A test drives it with a fake
    resolver + an in-process transport factory (offline).
    """

    def __init__(
        self,
        *,
        auth_resolver: AuthResolver,
        connect_timeout_seconds: float,
        call_timeout_seconds: float,
        user_agent: str,
        allowed_transports: frozenset[McpTransport] | None = None,
        endpoint_allowlist: frozenset[str] = frozenset(),
        rate_limit: RateLimitCheck | None = None,
        inner_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth_resolver = auth_resolver
        self._connect_timeout_seconds = connect_timeout_seconds
        self._call_timeout_seconds = call_timeout_seconds
        self._user_agent = user_agent
        self._allowed_transports = (
            allowed_transports if allowed_transports is not None else frozenset(McpTransport)
        )
        self._endpoint_allowlist = endpoint_allowlist
        self._rate_limit = rate_limit
        # A test seam: swap the real httpx transport for an in-process double
        # (e.g. ASGITransport onto a fixture MCP server) while the SSRF guard still
        # wraps it. Production leaves this ``None`` (a real network transport).
        self._inner_transport = inner_transport

    async def connect(self, server: McpServerConfig) -> McpSession:
        """Open + handshake a guarded transport to ``server``; return a session.

        Steps (all failures contained — the only exception raised is
        :class:`McpConnectionFailed`):

        1. **Transport allowed?** — a transport not in the configured allow-list is
           refused (deny by default);
        2. **Endpoint allowlist (optional)** — if an admin allowlist is set, the
           host must be on it (narrowing only; the SSRF guard still applies);
        3. **Rate limit** — the per-tenant window is checked (if wired);
        4. **Auth** — the bearer token is resolved from CC-C in-process (never
           logged/returned) and attached to the transport;
        5. **Connect** — the SDK opens the transport through the SSRF-guarded httpx
           client and completes the MCP handshake, bounded by the connect timeout.

        Raises:
            McpConnectionFailed: any failure, carrying a stable ``error_code``.
        """
        if server.transport not in self._allowed_transports:
            raise McpConnectionFailed(
                MCP_ERROR_UNAVAILABLE,
                f"transport {server.transport.value!r} is not enabled",
            )
        self._enforce_allowlist(server)
        self._enforce_rate_limit(server)

        headers = await self._resolve_auth_headers(server)

        exit_stack = AsyncExitStack()
        try:
            session = await asyncio.wait_for(
                self._open_session(server, headers, exit_stack),
                timeout=self._connect_timeout_seconds,
            )
        except McpConnectError as exc:
            await exit_stack.aclose()
            raise McpConnectionFailed(exc.error_code, exc.detail) from exc
        except TimeoutError as exc:
            await exit_stack.aclose()
            _log.warning("mcp.connect_timeout", slug=server.slug)
            raise McpConnectionFailed(
                MCP_ERROR_TIMEOUT, f"connecting to MCP server {server.slug!r} timed out"
            ) from exc
        except McpEgressBlockedError as exc:
            await exit_stack.aclose()
            _log.warning("mcp.connect_egress_blocked", slug=server.slug)
            raise McpConnectionFailed(MCP_ERROR_EGRESS_BLOCKED, str(exc)) from exc
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - contain everything
            await exit_stack.aclose()
            # An egress rejection can arrive wrapped by the SDK's exception-group
            # machinery; unwrap to preserve the specific reason/code.
            code, detail = _classify_connect_failure(exc, server.slug)
            _log.warning("mcp.connect_failed", slug=server.slug, error=type(exc).__name__)
            raise McpConnectionFailed(code, detail) from exc

        return McpSession(
            slug=server.slug,
            session=session,
            exit_stack=exit_stack,
            call_timeout_seconds=self._call_timeout_seconds,
        )

    async def list_tools(self, server: McpServerConfig) -> list[McpToolSpec]:
        """Connect, discover, and disconnect — the one-shot discovery helper.

        A contained-failure convenience over ``connect`` + ``list_tools``: any
        connect failure yields an empty list (the reason surfaces via
        :meth:`health`). Callers that hold a session open use
        :meth:`McpSession.list_tools` directly.
        """
        try:
            async with await self.connect(server) as session:
                return await session.list_tools()
        except McpConnectionFailed as exc:
            _log.warning("mcp.list_tools_connect_failed", slug=server.slug, code=exc.error_code)
            return []

    async def call_tool(
        self, server: McpServerConfig, name: str, args: dict[str, Any]
    ) -> McpToolResult:
        """Connect, invoke one tool, disconnect — the one-shot invoke helper.

        Any connect failure is mapped to a typed ``ok=False`` result carrying the
        connect ``error_code`` (never a raise). A tool-level failure is contained by
        :meth:`McpSession.call_tool`.
        """
        try:
            session = await self.connect(server)
        except McpConnectionFailed as exc:
            return McpToolResult.failure(
                error_code=exc.error_code,
                content=f"could not reach MCP server {server.slug!r}: {exc.detail}",
            )
        try:
            async with session:
                return await session.call_tool(name, args)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - contain everything
            _log.warning(
                "mcp.call_tool_teardown_failed", slug=server.slug, error=type(exc).__name__
            )
            return McpToolResult.failure(
                error_code=MCP_ERROR_UNAVAILABLE,
                content=f"the MCP call to {server.slug!r} failed",
            )

    async def health(self, server: McpServerConfig) -> McpHealth:
        """A bounded liveness/handshake probe; return a domain :class:`McpHealth`.

        Connects (handshake + capability negotiation), lists tools, and reports
        ``ready`` with the advertised tool count, or ``error`` with a safe,
        credential-free detail. Never raises.
        """
        try:
            async with await self.connect(server) as session:
                tools = await session.list_tools()
        except McpConnectionFailed as exc:
            return McpHealth(state=McpHealthState.ERROR, detail=exc.detail)
        return McpHealth(
            state=McpHealthState.READY,
            detail="handshake ok",
            tool_count=len(tools),
        )

    # --- internals ----------------------------------------------------------

    def _enforce_allowlist(self, server: McpServerConfig) -> None:
        """Refuse a host not on the (optional) admin endpoint allowlist.

        Empty allowlist ⇒ no narrowing (the SSRF guard is the mandatory control).
        A set allowlist is deny-by-default: only listed hosts pass (and still face
        the full SSRF range check — the allowlist never widens access).
        """
        if not self._endpoint_allowlist:
            return
        host = httpx.URL(server.endpoint_url).host
        if host not in self._endpoint_allowlist:
            raise McpConnectionFailed(
                MCP_ERROR_EGRESS_BLOCKED,
                f"host {host!r} is not on the MCP endpoint allowlist",
            )

    def _enforce_rate_limit(self, server: McpServerConfig) -> None:
        """Refuse a connect that exceeds the per-tenant MCP egress window (if wired)."""
        if self._rate_limit is None:
            return
        if not self._rate_limit():
            raise McpConnectionFailed(
                MCP_ERROR_RATE_LIMITED,
                f"MCP egress rate limit exceeded for {server.slug!r}",
            )

    async def _resolve_auth_headers(self, server: McpServerConfig) -> dict[str, str]:
        """Resolve the CC-C secret to a bearer header at connect time (never logged).

        Returns an empty dict for an anonymous server (no ``auth_secret_ref``). The
        resolved token is attached as ``Authorization: Bearer …`` and never logged,
        returned, or serialized.
        """
        if server.auth_secret_ref is None:
            return {}
        token = await self._auth_resolver(server.auth_secret_ref)
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    async def _open_session(
        self,
        server: McpServerConfig,
        headers: dict[str, str],
        exit_stack: AsyncExitStack,
    ) -> ClientSession:
        """Open the guarded transport + initialize the MCP session (SDK-facing).

        The one place the SDK transport is opened. The guarded httpx client factory
        (SSRF-validated + IP-pinned per hop) is injected so the guard is on the
        path the SDK actually uses (ADR-0012 §4). Raises :class:`McpConnectError`
        with a code on a protocol/transport failure — caught by :meth:`connect`.
        """
        client_factory = partial(
            build_guarded_client,
            default_timeout_seconds=self._connect_timeout_seconds,
            user_agent=self._user_agent,
            inner_transport=self._inner_transport,
        )
        try:
            if server.transport is McpTransport.STREAMABLE_HTTP:
                read, write, _get_id = await exit_stack.enter_async_context(
                    streamablehttp_client(
                        server.endpoint_url,
                        headers=headers or None,
                        timeout=self._connect_timeout_seconds,
                        httpx_client_factory=client_factory,
                    )
                )
            else:  # McpTransport.SSE
                read, write = await exit_stack.enter_async_context(
                    sse_client(
                        server.endpoint_url,
                        headers=headers or None,
                        timeout=self._connect_timeout_seconds,
                        httpx_client_factory=client_factory,
                    )
                )
            session = await exit_stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self._call_timeout_seconds),
                )
            )
            await session.initialize()
        except McpEgressBlockedError as exc:
            raise McpConnectError(MCP_ERROR_EGRESS_BLOCKED, str(exc)) from exc
        except (Exception, asyncio.CancelledError) as exc:
            code, detail = _classify_connect_failure(exc, server.slug)
            raise McpConnectError(code, detail) from exc
        return session


def _egress_block_in(exc: BaseException) -> McpEgressBlockedError | None:
    """Find an :class:`McpEgressBlockedError` in ``exc`` or its exception group.

    The SDK's anyio task groups surface a transport failure as an
    ``ExceptionGroup``; the SSRF rejection we raised inside the transport is nested
    there. Unwrap it so a blocked egress is reported as ``mcp_egress_blocked`` and
    never mis-labelled as a generic ``unavailable``.
    """
    if isinstance(exc, McpEgressBlockedError):
        return exc
    inner = getattr(exc, "exceptions", None)
    if inner:
        for sub in inner:
            found = _egress_block_in(sub)
            if found is not None:
                return found
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        return _egress_block_in(cause)
    return None


def _classify_connect_failure(exc: BaseException, slug: str) -> tuple[str, str]:
    """Map an arbitrary connect exception to a stable ``(error_code, detail)``.

    A nested SSRF rejection ⇒ ``mcp_egress_blocked`` with its reason; anything else
    ⇒ ``mcp_unavailable`` with a safe, credential-free message (the message names
    only the server slug + the exception *type*, never its args, which could echo a
    URL/token).
    """
    egress = _egress_block_in(exc)
    if egress is not None:
        return MCP_ERROR_EGRESS_BLOCKED, str(egress)
    return (
        MCP_ERROR_UNAVAILABLE,
        f"MCP server {slug!r} is unavailable ({type(exc).__name__})",
    )


def _to_tool_spec(tool: Tool) -> McpToolSpec:
    """Map an SDK :class:`Tool` to the domain :class:`McpToolSpec` (no vendor leak)."""
    read_only = bool(tool.annotations and tool.annotations.readOnlyHint)
    return McpToolSpec(
        name=tool.name,
        description=tool.description or "",
        input_schema=dict(tool.inputSchema or {}),
        read_only=read_only,
    )


def _to_tool_result(name: str, result: CallToolResult) -> McpToolResult:
    """Map an SDK :class:`CallToolResult` to the domain :class:`McpToolResult`.

    Concatenates the text content blocks into ``content`` (non-text blocks are
    summarised, never raised). An MCP application-level error (``isError``) becomes
    a typed ``ok=False`` result (``mcp_tool_error``) — the model reads it and the
    run continues.
    """
    content = _render_content(result.content)
    if result.isError:
        return McpToolResult(
            ok=False,
            content=content or f"the MCP tool {name!r} reported an error",
            is_error=True,
            error_code=MCP_ERROR_TOOL_ERROR,
            structured=result.structuredContent,
        )
    return McpToolResult(
        ok=True,
        content=content,
        is_error=False,
        structured=result.structuredContent,
    )


def _render_content(blocks: list[Any]) -> str:
    """Flatten the content blocks of a tool result into readable text.

    Text blocks are joined verbatim; a non-text block (image/embedded resource) is
    rendered as a short ``[type]`` placeholder so the model still learns something
    was returned. No vendor object is exposed — only the derived string.
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{getattr(block, 'type', 'content')}]")
    return "\n".join(parts)


__all__ = [
    "McpClient",
    "McpConnectionFailed",
    "McpSession",
    "RateLimitCheck",
]
