"""An in-process fixture MCP server for the ``app/mcp/`` adapter tests (offline).

Deliberately **without** ``from __future__ import annotations``: FastMCP inspects
a tool function's real (non-stringized) parameter annotations to build its JSON
schema, so the tool signatures here must keep their concrete types. Kept out of a
``test_*`` module so pytest does not collect it as tests.

Exposes a tiny server with a read-only ``echo`` tool and an always-raising
``boom`` tool (the server-side application-error path), plus a small context
manager that mounts it as an ASGI app and runs its lifespan so the streamable-HTTP
session manager is initialised.
"""

import httpx

from mcp.server.fastmcp import FastMCP


def build_fixture_server() -> FastMCP:
    """A minimal FastMCP server: one read-only tool + one erroring tool."""
    server = FastMCP("fixture", stateless_http=True, json_response=True)

    @server.tool()
    def echo(text: str) -> str:
        """Echo the text back (a read-only tool)."""
        return f"echo: {text}"

    @server.tool()
    def boom() -> str:
        """A tool that always raises (server-side application error)."""
        raise RuntimeError("intentional tool failure")

    return server


class FixtureMcp:
    """Mounts a FastMCP server as an in-memory ASGI app for the duration of a test.

    ``inner_transport`` is the in-memory :class:`httpx.ASGITransport` an
    :class:`~app.mcp.client.McpClient` uses as the SSRF guard's inner transport, so
    the full connect/list/call round-trip runs with **no socket**. Enter/exit runs
    the app's lifespan so the streamable-HTTP session-manager task group starts.
    """

    def __init__(self, server: FastMCP) -> None:
        self._app = server.streamable_http_app()
        self._lifespan_cm = self._app.router.lifespan_context(self._app)
        self.inner_transport = httpx.ASGITransport(app=self._app)

    async def __aenter__(self) -> "FixtureMcp":
        await self._lifespan_cm.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._lifespan_cm.__aexit__(None, None, None)


def fixture_mcp() -> FixtureMcp:
    """A fresh fixture server (its own in-memory transport) per call."""
    return FixtureMcp(build_fixture_server())
