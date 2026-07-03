"""Discovered MCP tools → governed CC-A tool definitions (issue #227, ADR-0012 §6).

The bridge that turns a tenant's **registered + enabled** MCP servers' discovered
tools into :class:`~app.services.tools.types.ToolDefinition`s so they flow through
the *same* invoke → allow-list → risk-tier → approval → audit → trace path as a
native tool (:class:`~app.services.tools.runner.ToolRunner`). MCP adds no second
tool pipeline.

**Namespaced, tenant-scoped, dynamic.** Each discovered tool becomes
``mcp:<server_slug>:<tool>`` (the same slug the #226 service derives). MCP tools
are per-tenant and change as servers are registered/toggled/re-discovered, so they
are resolved **per run** from the caller's own servers — never a global static
registration that could leak an MCP server across tenants (INV-1). The resolved
map is handed to the runner as its ``extra_tools`` for that one answer.

**Risk-tiering (ADR-0012 §6, INV-7 — trust is earned).** A tool the server
annotates read-only maps to **T0** (frictionless, no approval). Anything else — a
write-capable or unannotated external tool — defaults to **T2**, a write tier that
``requires_approval``: deny-by-default for third-party tools, gated by the CC-A
approval hook before any outbound call. ``default_offered=False`` on every MCP
tool, so an MCP tool is offered to a run **only** when the assistant's allow-list
names it (§ per-assistant selection).

**Failure isolation + audit inherit from the runner/adapter.** The handler calls
:meth:`app.mcp.McpClient.call_tool`, which contains every failure mode (down /
timeout / protocol fault) as a typed ``ok=False`` :class:`McpToolResult`; the
handler maps that to a :class:`~app.domain.tools.ToolHandlerResult`. The runner
bounds, audits (``tool.invoked`` / ``tool.result``), and records the
``tool_invocations`` row for every outcome — the same records it writes for a
native tool (INV-6). Arguments are schema-validated **before** the outbound call
(a malformed call never reaches the server — INV-8).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.domain.entities import McpServer
from app.domain.tools import ERROR_BAD_ARGS, RiskTier, ToolHandlerResult
from app.mcp import McpServerConfig, McpToolResult, McpTransport
from app.services.tools.types import ToolContext, ToolDefinition

# The registry namespace prefix for every discovered MCP tool. Kept here (not a
# bare literal) so the allow-list validator and the assistant-runtime resolver can
# recognise an ``mcp:*`` id without importing the whole bridge.
MCP_TOOL_PREFIX = "mcp:"

# A per-call invoke seam: given a resolved :class:`McpServerConfig`, the raw
# (server-advertised) tool name, and the model-supplied args, return a typed
# :class:`McpToolResult` (never a raise — the adapter contains every failure). The
# real binding is :meth:`app.mcp.McpClient.call_tool`; a test passes a fake so the
# whole bridge runs offline without a live server.
McpInvoker = Callable[[McpServerConfig, str, dict[str, Any]], Awaitable[McpToolResult]]


def slug_for_server(server: McpServer) -> str:
    """The stable, log-safe slug for a server (mirrors the #226 service).

    Derived from the server id so it is unique per server and never carries a
    credential or a user-controlled string into a registry name / structured log.
    Kept identical to ``mcp_servers_service._slug_for`` so the namespaced name a
    tool is registered under here matches the one the ``/mcp-servers`` surface
    projects.
    """
    return f"srv-{server.id.hex[:12]}"


def namespaced_tool_name(slug: str, raw_name: str) -> str:
    """The ``mcp:<slug>:<tool>`` registry name for a discovered tool (ADR-0012 §6)."""
    return f"{MCP_TOOL_PREFIX}{slug}:{raw_name}"


def is_mcp_tool_name(name: str) -> bool:
    """Whether ``name`` is a namespaced MCP tool id (``mcp:<slug>:<tool>``)."""
    return name.startswith(MCP_TOOL_PREFIX)


def _config_for(server: McpServer) -> McpServerConfig:
    """Build the adapter connection config from a persisted row (no credential).

    The ``auth_secret_ref`` is passed as an opaque handle; the adapter resolves it
    to a plaintext token in-process at connect time via the injected resolver — it
    is never read here and never placed on the config (ADR-0012 §3).
    """
    return McpServerConfig(
        slug=slug_for_server(server),
        endpoint_url=server.endpoint_url,
        transport=McpTransport(server.transport),
        auth_secret_ref=server.auth_secret_ref,
    )


def _validate_args(args: dict[str, Any], input_schema: dict[str, Any]) -> str | None:
    """A minimal JSON-Schema pre-check of ``args``; a reason string on failure, else ``None``.

    The malformed-input boundary (ADR-0012 §6 / INV-8): a call whose args do not fit
    the tool's **advertised** ``input_schema`` is rejected *before* any outbound hop,
    so a bad call never reaches the server (and never spends the egress budget). This
    is a deliberately small, dependency-free check — the common failure shapes the
    model actually produces — not a full validator:

    * the top level must be an object when the schema says ``type: object``;
    * every ``required`` property must be present and non-null;
    * a present property whose schema names a primitive ``type`` must match it.

    Anything the check does not understand is permitted through (the server still
    validates authoritatively and any rejection there is a contained ``ok=False``
    result). An empty/absent schema imposes no constraint.
    """
    if not input_schema:
        return None
    schema_type = input_schema.get("type")
    if schema_type == "object" and not isinstance(args, dict):
        return "arguments must be a JSON object"

    required = input_schema.get("required")
    if isinstance(required, list):
        for key in required:
            if not isinstance(key, str):
                continue
            if key not in args or args[key] is None:
                return f"missing required argument {key!r}"

    properties = input_schema.get("properties")
    if isinstance(properties, dict):
        for key, prop_schema in properties.items():
            if key not in args or args[key] is None:
                continue
            if not isinstance(prop_schema, dict):
                continue
            reason = _check_primitive_type(key, args[key], prop_schema.get("type"))
            if reason is not None:
                return reason
    return None


def _check_primitive_type(key: str, value: object, declared: object) -> str | None:
    """Reject ``value`` when the schema names a primitive ``type`` it does not match.

    Only the JSON-Schema primitives with an unambiguous Python mapping are checked;
    an unknown / composite / list-of-types declaration is left to the server. Note
    ``bool`` is intentionally NOT an ``integer`` (a common trap), and an ``integer``
    schema accepts an int but not a float.
    """
    checks: dict[str, Callable[[object], bool]] = {
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    if not isinstance(declared, str):
        return None
    predicate = checks.get(declared)
    if predicate is None:
        return None
    if not predicate(value):
        return f"argument {key!r} must be of type {declared}"
    return None


def _map_result(result: McpToolResult) -> ToolHandlerResult:
    """Fold a contained :class:`McpToolResult` into a CC-A :class:`ToolHandlerResult`.

    A success carries the flattened text content back to the model; a contained
    failure (server down, timeout, protocol fault, or an application-level MCP tool
    error) becomes an ``ok=False`` handler body whose ``error`` is the adapter's
    stable ``MCP_ERROR_*`` code — the model reads it and the run continues (the
    runner never crashes on it, ADR-0012 §7). The structured content (if any) is
    carried in the payload for the trace.
    """
    payload: dict[str, Any] = {}
    if result.structured is not None:
        payload["structured"] = result.structured
    if result.ok:
        return ToolHandlerResult(
            content=result.content,
            summary="mcp tool ok",
            payload=payload,
        )
    return ToolHandlerResult(
        content=result.content or "the MCP tool failed",
        ok=False,
        error=result.error_code,
        summary="mcp tool error",
        payload=payload,
    )


def _build_handler(
    server: McpServer,
    raw_name: str,
    input_schema: dict[str, Any],
    invoker: McpInvoker,
) -> Callable[[dict[str, Any], ToolContext], Awaitable[ToolHandlerResult]]:
    """Bind one discovered tool to a CC-A handler that delegates to ``app.mcp``.

    The returned handler validates the model-supplied args against the advertised
    schema (bad args → a typed ``ok=False`` ``tool_bad_args`` body, no outbound
    call), then invokes the tool via the injected :data:`McpInvoker` (the adapter's
    ``call_tool`` — auth resolved from the vault at call time, every failure
    contained), and maps the :class:`McpToolResult` back. It never raises for a tool
    concern; the runner's bounded-execute wrapper is the backstop for a genuinely
    unexpected error.
    """
    config = _config_for(server)

    async def _handler(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
        reason = _validate_args(args, input_schema)
        if reason is not None:
            return ToolHandlerResult(
                content=f"The MCP tool arguments are invalid: {reason}.",
                ok=False,
                error=ERROR_BAD_ARGS,
                summary="invalid mcp args",
            )
        result = await invoker(config, raw_name, args)
        return _map_result(result)

    return _handler


def _definition_for(
    server: McpServer,
    raw: dict[str, object],
    invoker: McpInvoker,
) -> ToolDefinition | None:
    """Build one namespaced :class:`ToolDefinition` from a discovered-tool snapshot.

    Risk-tiering (ADR-0012 §6): a server-annotated read-only tool → **T0** (no
    approval); anything else → **T2**, a write tier that ``requires_approval``
    (deny-by-default for external/write-capable tools, INV-7). ``default_offered``
    is always ``False`` — an MCP tool is offered only when an assistant's allow-list
    names it. Returns ``None`` for a nameless snapshot entry (a corrupt row is
    skipped, never a crash).
    """
    raw_name = str(raw.get("name", "")).strip()
    if not raw_name:
        return None
    read_only = bool(raw.get("read_only", False))
    schema = raw.get("input_schema")
    input_schema: dict[str, Any] = schema if isinstance(schema, dict) else {}
    description = raw.get("description")
    slug = slug_for_server(server)

    handler = _build_handler(server, raw_name, input_schema, invoker)
    if read_only:
        return ToolDefinition(
            name=namespaced_tool_name(slug, raw_name),
            description=str(description) if description is not None else "",
            json_schema=input_schema,
            handler=handler,
            risk_tier=RiskTier.T0,
            requires_approval=False,
            read_only=True,
            default_offered=False,
        )
    return ToolDefinition(
        name=namespaced_tool_name(slug, raw_name),
        description=str(description) if description is not None else "",
        json_schema=input_schema,
        handler=handler,
        risk_tier=RiskTier.T2,
        requires_approval=True,
        read_only=False,
        default_offered=False,
    )


def tools_for_servers(
    servers: list[McpServer],
    invoker: McpInvoker,
) -> dict[str, ToolDefinition]:
    """The namespaced :class:`ToolDefinition` map for a set of registered servers.

    Only **enabled** servers contribute (a disabled server's tools are never
    offered or invokable — the enforcement is in this one place, ADR-0012 §6). Each
    contributes the tools in its persisted ``discovered_tools`` snapshot, namespaced
    ``mcp:<slug>:<tool>`` and risk-tiered. A duplicate namespaced name across the
    set (impossible in practice — the slug is per-server) keeps the first, so the
    map is well-formed. Pure over the passed rows + invoker, so the caller owns the
    tenant scoping (it lists only the caller's own servers) — this never queries.
    """
    resolved: dict[str, ToolDefinition] = {}
    for server in servers:
        if not server.enabled:
            continue
        for raw in server.discovered_tools:
            if not isinstance(raw, dict):
                continue
            definition = _definition_for(server, raw, invoker)
            if definition is None:
                continue
            resolved.setdefault(definition.name, definition)
    return resolved


__all__ = [
    "MCP_TOOL_PREFIX",
    "McpInvoker",
    "is_mcp_tool_name",
    "namespaced_tool_name",
    "slug_for_server",
    "tools_for_servers",
]
