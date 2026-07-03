"""AC-2 — the MCP SSRF egress guard rejects blocked targets on connect AND redirect.

The load-bearing control (ADR-0012 §4, risk:security): every outbound MCP hop —
the initial connect **and every redirect the SDK follows** — passes the same SSRF
discipline as ``connectors/web/fetch.py`` (https-only, reject loopback/private/
link-local/metadata, IP-pinned). Because httpx re-invokes the transport once per
hop, the guard sits on the SDK's actual path: a public URL that 30x-redirects to
``127.0.0.1`` or the cloud-metadata IP is refused at the redirect, not followed.

These tests drive the guard transport directly with an ``httpx.AsyncClient`` (no
MCP SDK needed) so the per-hop behaviour is asserted in isolation.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from app.mcp.egress import (
    McpEgressBlockedError,
    _SsrfGuardTransport,
    build_guarded_client,
)

# A public-looking host the tests use; its resolution is stubbed per test.
_FIXTURE_HOST = "mcp.example.com"


def _guarded_client(inner: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    """A client whose SSRF guard wraps ``inner`` (a test double for the real socket)."""
    return build_guarded_client(
        headers=None,
        timeout=None,
        auth=None,
        default_timeout_seconds=5.0,
        user_agent="LumenCopilot-Test/1",
        inner_transport=inner,
    )


class _RecordingTransport(httpx.AsyncBaseTransport):
    """An inner transport that records the (pinned) URLs it is asked to open.

    Stands in for the real ``AsyncHTTPTransport``. It never opens a socket; it
    returns a canned 200 (or a 302 to ``redirect_to`` on the first hop) so the
    guard's per-hop behaviour is observable without any network.
    """

    def __init__(self, *, redirect_to: str | None = None) -> None:
        self.seen: list[str] = []
        self._redirect_to = redirect_to

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        if self._redirect_to is not None and len(self.seen) == 1:
            return httpx.Response(302, headers={"location": self._redirect_to})
        return httpx.Response(200, text="ok")


async def test_connect_to_loopback_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host resolving to loopback is refused on connect (never reaches the socket)."""

    def _resolve_loopback(*_a: object, **_k: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_loopback)
    inner = _RecordingTransport()
    async with _guarded_client(inner) as client:
        with pytest.raises(McpEgressBlockedError):
            await client.get(f"https://{_FIXTURE_HOST}/mcp")
    assert inner.seen == [], "a blocked target must never reach the inner transport"


async def test_connect_to_metadata_ip_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cloud-metadata address (169.254.169.254) is refused (SSRF pivot)."""

    def _resolve_metadata(*_a: object, **_k: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_metadata)
    inner = _RecordingTransport()
    async with _guarded_client(inner) as client:
        with pytest.raises(McpEgressBlockedError):
            await client.get(f"https://{_FIXTURE_HOST}/mcp")
    assert inner.seen == []


async def test_connect_to_private_ip_literal_is_rejected() -> None:
    """A private-range IP literal is refused without any DNS (range-checked directly)."""
    inner = _RecordingTransport()
    async with _guarded_client(inner) as client:
        with pytest.raises(McpEgressBlockedError):
            await client.get("https://10.0.0.5/mcp")
    assert inner.seen == []


async def test_non_https_scheme_is_rejected() -> None:
    """A non-https MCP endpoint is refused (stricter than the web connector)."""
    inner = _RecordingTransport()
    async with _guarded_client(inner) as client:
        with pytest.raises(McpEgressBlockedError):
            await client.get("http://public.example.com/mcp")
    assert inner.seen == []


async def test_redirect_to_loopback_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public host that 302-redirects to loopback is refused ON THE REDIRECT HOP.

    The critical case (ADR-0012 §4): the first hop resolves to a public IP and is
    allowed; the server then redirects to ``https://127.0.0.1/evil``. httpx follows
    it by re-invoking the transport, where the guard re-validates the *redirect
    target* and refuses it — the redirect is never followed to the blocked address.
    """
    real_getaddrinfo = socket.getaddrinfo

    def _resolve(host: str, *args: object, **kwargs: object) -> list:
        if host == _FIXTURE_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        if host == "127.0.0.1":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return real_getaddrinfo(host, *args, **kwargs)  # pragma: no cover

    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    inner = _RecordingTransport(redirect_to="https://127.0.0.1/evil")
    async with _guarded_client(inner) as client:
        with pytest.raises(McpEgressBlockedError):
            await client.get(f"https://{_FIXTURE_HOST}/mcp")
    # The first (public) hop was allowed through to the inner transport; the
    # redirect target was refused BEFORE a second inner call.
    assert inner.seen == [
        "https://93.184.216.34/mcp"
    ], "only the validated first hop reaches the socket; the loopback redirect is blocked"


async def test_allowed_public_host_is_pinned_to_its_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A permitted public host connects to the VALIDATED IP with Host/SNI preserved.

    The happy path also proves the TOCTOU pin: the socket URL is rewritten to the
    resolved IP, while the original host is preserved in the ``Host`` header and the
    ``sni_hostname`` extension (so TLS/virtual-hosting still work).
    """

    def _resolve_public(*_a: object, **_k: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_public)

    seen_requests: list[httpx.Request] = []

    class _Capturing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            return httpx.Response(200, text="ok")

    async with _guarded_client(_Capturing()) as client:
        resp = await client.get(f"https://{_FIXTURE_HOST}/mcp")
    assert resp.status_code == 200
    req = seen_requests[0]
    assert req.url.host == "93.184.216.34", "the connection is pinned to the validated IP"
    assert req.headers["Host"] == _FIXTURE_HOST, "the original host is preserved for routing"
    assert req.extensions.get("sni_hostname") == _FIXTURE_HOST, "TLS SNI uses the real host"


def test_ssrf_guard_transport_wraps_an_inner_transport() -> None:
    """The guard is a real httpx transport wrapping the inner one (structural)."""
    inner = _RecordingTransport()
    guard = _SsrfGuardTransport(inner)
    assert isinstance(guard, httpx.AsyncBaseTransport)
