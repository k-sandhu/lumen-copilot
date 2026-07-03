"""Domain types the MCP adapter exposes upward — **no vendor type crosses here**.

The ``app/mcp/`` boundary (ADR-0012 §2) speaks only these plain, framework-free
shapes: the transport enum, the server-connection config (``McpServerConfig``),
the auth resolver seam, the normalized tool spec/result, and the health probe
result. No MCP-SDK object, JSON-RPC frame, or protocol schema type ever leaves
the module — a caller of ``app/mcp/`` sees exactly this vocabulary and nothing
else (the same rule ``llm/`` and ``search/`` follow).

This issue (#225) is the **client adapter only** — the ``mcp_servers``
registration table + API is #226. So the connection input here is a lightweight
domain :class:`McpServerConfig` (what the adapter needs to open a connection),
not a DB row; #226 maps its persisted row into this config and supplies the
auth-secret resolver.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class McpTransport(str, enum.Enum):
    """The remote MCP transport to open (ADR-0012 §1 — remote only in v1).

    ``STREAMABLE_HTTP`` is the primary current transport; ``SSE`` is the earlier
    remote transport kept for compatibility. ``stdio``/local-process is **not**
    a member — it is deferred behind the code-execution sandbox (ADR-0013), so
    there is no way to name it here (registering it is impossible by type).
    """

    STREAMABLE_HTTP = "streamable_http"
    SSE = "sse"


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """What the adapter needs to open a connection to one remote MCP server.

    A domain input (not a DB row): #226 builds this from a persisted
    ``mcp_servers`` row and passes it in. ``endpoint_url`` is the remote https
    endpoint (SSRF-checked on connect and every redirect hop); ``transport``
    selects Streamable-HTTP or SSE. ``auth_secret_ref`` is an **opaque handle**
    to a CC-C secret (never the secret itself) — the adapter resolves it to a
    plaintext bearer token *at call time* via the injected resolver (§3) and
    never logs or returns it. ``slug`` is a stable, log-safe identifier for the
    server used in structured logs/health (never carries credentials).
    """

    slug: str
    endpoint_url: str
    transport: McpTransport
    auth_secret_ref: str | None = None


# The auth resolver seam (CC-C #209). The adapter is handed a callable that, given
# a server's ``auth_secret_ref``, returns the plaintext bearer token to attach —
# resolved from the secrets vault **in-process at call time**, never held on the
# config. #226 wires this to ``SecretsService.get_secret_plaintext``; tests pass a
# fake. Returning ``None`` means "no auth" (an anonymous server). The adapter
# never logs the returned value and never places it in a result.
AuthResolver = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class McpToolSpec:
    """One tool a server advertises, normalized to the domain (ADR-0012 §2).

    The SDK's tool descriptor mapped to plain fields: ``name`` (as the server
    advertises it — the caller namespaces it ``mcp:<slug>:<name>`` when injecting
    into the CC-A registry, #227), ``description``, the ``input_schema`` JSON
    Schema for its arguments, and ``read_only`` — ``True`` only when the server
    annotates the tool read-only/non-destructive (an ``readOnlyHint``-style
    annotation). Unknown/unannotated ⇒ ``read_only=False`` (trust is earned;
    #227 maps that to the default T2 tier). No MCP schema object leaks — this is
    all a caller sees.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class McpToolResult:
    """The uniform result of one ``call_tool`` — success or failure, never a raise.

    Every outcome (a tool that ran, a server that was down, a timeout, a
    malformed/blocked request) is *this* shape (ADR-0012 §7): ``ok`` is the
    headline; ``content`` is the text the model reads back; ``is_error`` reflects
    the MCP protocol's own tool-level error flag on an otherwise-delivered call;
    ``error_code`` is one of the ``MCP_ERROR_*`` codes set **iff** ``ok`` is
    False. A failure NEVER escapes the module as an exception — the caller (#227)
    turns this into a CC-A ``ToolResult``.

    Invariant: ``ok`` XOR ``error_code`` — ``ok=True`` ⇒ ``error_code is None``;
    ``ok=False`` ⇒ ``error_code`` is a non-empty code.
    """

    ok: bool
    content: str = ""
    is_error: bool = False
    error_code: str | None = None
    structured: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.ok and self.error_code is not None:
            raise ValueError("a successful McpToolResult must not carry an error code")
        if not self.ok and not self.error_code:
            raise ValueError("a failed McpToolResult must carry a non-empty error code")

    @classmethod
    def failure(cls, *, error_code: str, content: str) -> McpToolResult:
        """Build an ``ok=False`` result carrying ``error_code`` (a contained failure)."""
        return cls(ok=False, content=content, is_error=True, error_code=error_code)


class McpHealthState(str, enum.Enum):
    """The outcome of a bounded liveness/handshake probe (ADR-0012 §7)."""

    READY = "ready"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class McpHealth:
    """The result of :func:`app.mcp.health` — a server's reachability at a glance.

    ``state`` is ``ready`` when the handshake + capability negotiation succeeded,
    ``error`` otherwise. ``detail`` is a safe, credential-free message (an egress
    rejection, a timeout, a protocol fault) for the management UI; it never
    carries auth material. ``tool_count`` is how many tools the server advertised
    on a successful probe (``0`` on error).
    """

    state: McpHealthState
    detail: str = ""
    tool_count: int = 0

    @property
    def ok(self) -> bool:
        """True iff the probe reached ``ready``."""
        return self.state is McpHealthState.READY


# --- Contained-failure codes ------------------------------------------------
# The stable reasons a connect/list/call resolves to a failure instead of raising
# out of the module (ADR-0012 §7). Mirrors the CC-A tool-error taxonomy so #227
# can map straight across.

#: The outbound target was refused by the SSRF egress guard (blocked/private/
#: loopback/metadata address, on connect OR a redirect hop) — a blocking defect
#: if it were ever bypassed (ADR-0012 §4).
MCP_ERROR_EGRESS_BLOCKED = "mcp_egress_blocked"
#: A non-https endpoint for a remote server (stricter than the web connector —
#: MCP carries credentials, ADR-0012 §4).
MCP_ERROR_SCHEME = "mcp_scheme_not_allowed"
#: The transport/handshake failed — server down, refused, TLS error, protocol
#: fault, or malformed frame (ADR-0012 §7).
MCP_ERROR_UNAVAILABLE = "mcp_unavailable"
#: The call exceeded its wall-clock timeout (a slow server, ADR-0012 §7).
MCP_ERROR_TIMEOUT = "mcp_timeout"
#: The per-tenant MCP egress rate window is exhausted (ADR-0012 §4, throttled).
MCP_ERROR_RATE_LIMITED = "mcp_rate_limited"
#: The requested tool is not advertised by the server (deny by default).
MCP_ERROR_TOOL_NOT_FOUND = "mcp_tool_not_found"
#: The tool ran but reported an application-level error (MCP ``isError``).
MCP_ERROR_TOOL_ERROR = "mcp_tool_error"


__all__ = [
    "MCP_ERROR_EGRESS_BLOCKED",
    "MCP_ERROR_RATE_LIMITED",
    "MCP_ERROR_SCHEME",
    "MCP_ERROR_TIMEOUT",
    "MCP_ERROR_TOOL_ERROR",
    "MCP_ERROR_TOOL_NOT_FOUND",
    "MCP_ERROR_UNAVAILABLE",
    "AuthResolver",
    "McpHealth",
    "McpHealthState",
    "McpServerConfig",
    "McpToolResult",
    "McpToolSpec",
    "McpTransport",
]
