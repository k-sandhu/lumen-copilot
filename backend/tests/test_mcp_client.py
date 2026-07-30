"""AC-1/AC-3/AC-4 — the MCP client adapter against an in-process fixture server.

Fully offline: a real :class:`FastMCP` server (``tests/mcp_fixture_server.py``) is
mounted as an ASGI app and the **real** :class:`~app.mcp.client.McpClient` talks to
it through an in-memory ``httpx.ASGITransport`` — no socket, but the real MCP SDK
handshake, the real SSRF guard (its DNS stubbed to a public IP for the fixture
host), and the real domain-type mapping are all exercised.

Covers:
* **AC-1** — ``connect`` + ``list_tools`` + ``call_tool`` round-trip;
* **AC-3** — auth is pulled from the (fake) CC-C resolver at connect time and the
  token appears in NO log/result/trace;
* **AC-4** — a downed / erroring / slow server yields a typed ``ok=False`` result
  (or an ``error`` health), never an exception up-stack.
"""

from __future__ import annotations

import logging
import socket

import anyio
import httpx
import pytest

from app.mcp import (
    MCP_ERROR_TIMEOUT,
    MCP_ERROR_TOOL_ERROR,
    MCP_ERROR_UNAVAILABLE,
    AuthResolver,
    McpClient,
    McpHealthState,
    McpServerConfig,
    McpTransport,
)
from tests._mcp_fixture_server import FixtureMcp, fixture_mcp

# The fixture host the client points at; its DNS is stubbed to a public IP so the
# SSRF guard admits it (and then the ASGI inner transport serves the request).
_FIXTURE_HOST = "fixture-mcp.example.com"
_FIXTURE_URL = f"https://{_FIXTURE_HOST}/mcp"
_PUBLIC_IP = "93.184.216.34"


async def _throttled() -> bool:
    """An always-throttling rate-limit check (awaitable, #527)."""
    return False


@pytest.fixture(autouse=True)
def _stub_public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the fixture host to a PUBLIC IP so the SSRF guard admits the connect.

    The guard still runs in full (range check + IP-pin); we only make the fixture
    host look public so the round-trip reaches the in-process ASGI server. A test
    that wants a *blocked* target overrides this locally (see test_mcp_egress).
    """
    real = socket.getaddrinfo

    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        if host == _FIXTURE_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]
        return real(host, *args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)


async def _none_resolver(_ref: str) -> str | None:
    """A resolver that supplies no auth (anonymous server)."""
    return None


def _make_client(
    fixture: FixtureMcp,
    *,
    auth_resolver: AuthResolver | None = None,
    connect_timeout_seconds: float = 10.0,
    call_timeout_seconds: float = 10.0,
    allowed_transports: frozenset[McpTransport] | None = None,
) -> McpClient:
    """An ``McpClient`` wired to the fixture's in-process transport (offline)."""
    return McpClient(
        auth_resolver=auth_resolver or _none_resolver,
        connect_timeout_seconds=connect_timeout_seconds,
        call_timeout_seconds=call_timeout_seconds,
        user_agent="LumenCopilot-Test/1",
        allowed_transports=allowed_transports,
        inner_transport=fixture.inner_transport,
    )


def _config(*, auth_ref: str | None = None) -> McpServerConfig:
    return McpServerConfig(
        slug="fixture",
        endpoint_url=_FIXTURE_URL,
        transport=McpTransport.STREAMABLE_HTTP,
        auth_secret_ref=auth_ref,
    )


# --- AC-1: round-trip -------------------------------------------------------


async def test_connect_list_tools_call_tool_round_trip() -> None:
    """AC-1 — connect + list_tools + call_tool succeed against the fixture server."""
    async with fixture_mcp() as fixture:
        client = _make_client(fixture)
        async with await client.connect(_config()) as session:
            tools = await session.list_tools()
            names = {t.name for t in tools}
            assert {"echo", "boom"} <= names

            echo_spec = next(t for t in tools if t.name == "echo")
            assert echo_spec.description
            assert echo_spec.input_schema.get("type") == "object"

            result = await session.call_tool("echo", {"text": "hello"})
            assert result.ok is True
            assert result.error_code is None
            assert "echo: hello" in result.content


async def test_one_shot_call_tool_helper_round_trip() -> None:
    """AC-1 — the connect-invoke-disconnect one-shot helper returns an ok result."""
    async with fixture_mcp() as fixture:
        client = _make_client(fixture)
        result = await client.call_tool(_config(), "echo", {"text": "world"})
        assert result.ok is True
        assert "echo: world" in result.content


async def test_health_probe_reports_ready_with_tool_count() -> None:
    """AC-1 — health() handshakes and reports ready + the advertised tool count."""
    async with fixture_mcp() as fixture:
        client = _make_client(fixture)
        health = await client.health(_config())
        assert health.state is McpHealthState.READY
        assert health.ok is True
        assert health.tool_count >= 2


# --- AC-3: secret hygiene ---------------------------------------------------


async def test_auth_pulled_from_vault_at_call_time() -> None:
    """AC-3 — the auth token is resolved from the (fake) CC-C resolver at connect."""
    calls: list[str] = []

    async def _resolver(ref: str) -> str | None:
        calls.append(ref)
        return "super-secret-token"

    async with fixture_mcp() as fixture:
        client = _make_client(fixture, auth_resolver=_resolver)
        result = await client.call_tool(_config(auth_ref="secret-ref-123"), "echo", {"text": "x"})
    assert result.ok is True
    assert calls == ["secret-ref-123"], "the resolver was invoked once with the server's ref"


async def test_secret_never_appears_in_result_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    """AC-3 — the resolved token appears in NO result field and NO log record.

    Fetches a distinctive token, runs a full round-trip AND a forced failure, and
    asserts the token string is absent from every result repr and every captured
    log line (structured logs key only off the log-safe slug).
    """
    token = "TOK_LEAK_CANARY_9f8e7d"

    async def _resolver(_ref: str) -> str | None:
        return token

    caplog.set_level(logging.DEBUG)
    async with fixture_mcp() as fixture:
        client = _make_client(fixture, auth_resolver=_resolver)
        ok_result = await client.call_tool(_config(auth_ref="ref"), "echo", {"text": "hi"})
        # also a tool-error path, which logs — the token must not leak there either
        err_result = await client.call_tool(_config(auth_ref="ref"), "boom", {})

    assert token not in repr(ok_result)
    assert token not in repr(err_result)
    joined_logs = "\n".join(record.getMessage() for record in caplog.records)
    joined_logs += "\n" + "\n".join(str(getattr(r, "args", "")) for r in caplog.records)
    assert token not in joined_logs, "the auth token must never appear in a log record"


# --- AC-4: failure isolation ------------------------------------------------


async def test_tool_side_error_is_contained_as_ok_false() -> None:
    """AC-4 — a server-side tool that raises yields ok=False (mcp_tool_error)."""
    async with fixture_mcp() as fixture:
        client = _make_client(fixture)
        result = await client.call_tool(_config(), "boom", {})
    assert result.ok is False
    assert result.error_code == MCP_ERROR_TOOL_ERROR
    assert result.is_error is True


async def test_unknown_tool_is_contained_as_ok_false() -> None:
    """AC-4 — invoking a tool the server does not advertise is a contained failure."""
    async with fixture_mcp() as fixture:
        client = _make_client(fixture)
        result = await client.call_tool(_config(), "no_such_tool", {})
    assert result.ok is False
    assert result.error_code in {MCP_ERROR_TOOL_ERROR, MCP_ERROR_UNAVAILABLE}


async def test_downed_server_yields_ok_false_not_exception() -> None:
    """AC-4 — a server that refuses every connection yields ok=False, never a raise.

    The inner transport raises a ``ConnectError`` for every request (a down server);
    ``call_tool`` returns a typed ``ok=False`` result and ``health`` reports error.
    """

    class _DownTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    client = McpClient(
        auth_resolver=_none_resolver,
        connect_timeout_seconds=5.0,
        call_timeout_seconds=5.0,
        user_agent="LumenCopilot-Test/1",
        inner_transport=_DownTransport(),
    )
    result = await client.call_tool(_config(), "echo", {"text": "x"})
    assert result.ok is False
    assert result.error_code == MCP_ERROR_UNAVAILABLE

    health = await client.health(_config())
    assert health.state is McpHealthState.ERROR
    assert health.ok is False


async def test_slow_server_times_out_as_ok_false() -> None:
    """AC-4 — a server slower than the timeout yields a typed timeout/unavailable result."""

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await anyio.sleep(30)  # far beyond the tiny timeout below
            return httpx.Response(200, text="never")  # pragma: no cover

    client = McpClient(
        auth_resolver=_none_resolver,
        connect_timeout_seconds=0.25,
        call_timeout_seconds=0.25,
        user_agent="LumenCopilot-Test/1",
        inner_transport=_SlowTransport(),
    )
    result = await client.call_tool(_config(), "echo", {"text": "x"})
    assert result.ok is False
    # A stall on connect surfaces as a timeout; either bounded code is acceptable.
    assert result.error_code in {MCP_ERROR_TIMEOUT, MCP_ERROR_UNAVAILABLE}


async def test_disabled_transport_is_refused() -> None:
    """A transport not in the allow-list is refused (deny by default)."""
    async with fixture_mcp() as fixture:
        client = _make_client(
            fixture,
            allowed_transports=frozenset({McpTransport.SSE}),  # streamable_http disabled
        )
        result = await client.call_tool(_config(), "echo", {"text": "x"})
    assert result.ok is False


async def test_endpoint_allowlist_refuses_unlisted_host() -> None:
    """An admin allowlist narrows further: an unlisted host is refused (defence-in-depth).

    The allowlist never *widens* — it is an extra deny-by-default gate on top of the
    SSRF guard. A host not on it is refused before any connection is attempted.
    """
    from app.mcp import McpClient

    async with fixture_mcp() as fixture:
        client = McpClient(
            auth_resolver=_none_resolver,
            connect_timeout_seconds=5.0,
            call_timeout_seconds=5.0,
            user_agent="LumenCopilot-Test/1",
            endpoint_allowlist=frozenset({"allowed-only.example.com"}),
            inner_transport=fixture.inner_transport,
        )
        # _FIXTURE_HOST is not on the allowlist → refused.
        result = await client.call_tool(_config(), "echo", {"text": "x"})
    assert result.ok is False


async def test_rate_limit_refuses_when_window_exhausted() -> None:
    """An exhausted per-tenant window refuses the connect as a contained result."""
    from app.mcp import McpClient

    async with fixture_mcp() as fixture:
        client = McpClient(
            auth_resolver=_none_resolver,
            connect_timeout_seconds=5.0,
            call_timeout_seconds=5.0,
            user_agent="LumenCopilot-Test/1",
            rate_limit=_throttled,  # always throttle
            inner_transport=fixture.inner_transport,
        )
        result = await client.call_tool(_config(), "echo", {"text": "x"})
    assert result.ok is False
