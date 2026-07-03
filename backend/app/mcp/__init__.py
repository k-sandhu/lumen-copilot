"""``app.mcp`` — the MCP client adapter boundary (ADR-0012 §2, risk:security).

The single owning module for talking to remote **MCP (Model Context Protocol)**
servers: the ONLY importer of the official MCP Python SDK. Everything it exposes
upward is a **domain type** (``app.mcp.types``) — no SDK object, JSON-RPC frame,
or MCP schema type crosses this boundary (the same adapter rule ``llm/`` and
``search/`` follow). An architecture test asserts no module outside ``app.mcp``
imports the SDK.

Public surface:

* :class:`~app.mcp.client.McpClient` — ``connect`` / ``list_tools`` / ``call_tool``
  / ``health`` over Streamable-HTTP or SSE, each outbound hop through the SSRF
  egress guard (:mod:`app.mcp.egress`), auth resolved from CC-C at call time;
* the domain types (:class:`~app.mcp.types.McpServerConfig`,
  :class:`~app.mcp.types.McpToolSpec`, :class:`~app.mcp.types.McpToolResult`,
  :class:`~app.mcp.types.McpHealth`, :class:`~app.mcp.types.McpTransport`, and the
  ``MCP_ERROR_*`` failure codes).

This issue (#225) is the adapter only. Registration/secrets/health persistence is
#226; namespaced tools-into-the-CC-A-registry is #227.
"""

from __future__ import annotations

from app.mcp.client import McpClient, McpConnectionFailed, McpSession, RateLimitCheck
from app.mcp.types import (
    MCP_ERROR_EGRESS_BLOCKED,
    MCP_ERROR_RATE_LIMITED,
    MCP_ERROR_SCHEME,
    MCP_ERROR_TIMEOUT,
    MCP_ERROR_TOOL_ERROR,
    MCP_ERROR_TOOL_NOT_FOUND,
    MCP_ERROR_UNAVAILABLE,
    AuthResolver,
    McpHealth,
    McpHealthState,
    McpServerConfig,
    McpToolResult,
    McpToolSpec,
    McpTransport,
)

__all__ = [
    "MCP_ERROR_EGRESS_BLOCKED",
    "MCP_ERROR_RATE_LIMITED",
    "MCP_ERROR_SCHEME",
    "MCP_ERROR_TIMEOUT",
    "MCP_ERROR_TOOL_ERROR",
    "MCP_ERROR_TOOL_NOT_FOUND",
    "MCP_ERROR_UNAVAILABLE",
    "AuthResolver",
    "McpClient",
    "McpConnectionFailed",
    "McpHealth",
    "McpHealthState",
    "McpServerConfig",
    "McpSession",
    "McpToolResult",
    "McpToolSpec",
    "McpTransport",
    "RateLimitCheck",
]
