"""SSRF guard tests — the load-bearing chokepoint (#20, ADR-0009 §3, risk:security).

These are the **mandatory** negatives (ADR-0009 §3): every category the server
must refuse when fetching a user-supplied URL — loopback, private (RFC-1918),
link-local, the cloud-metadata IP, CGNAT, IPv6 loopback/ULA, IPv4-mapped
loopback, a non-http(s) scheme, and — critically — a **redirect** from a public
host to any of those (re-validated on every hop, never followed to a blocked
target). Plus the size cap and the content-type allowlist.

All offline: URL **validation** is pure (the loopback/private literals need no
network; the few host-name cases use no DNS here), and the **fetch** path is
driven by an ``httpx.MockTransport`` so no real socket opens. Every block raises
``UrlBlockedError`` (code ``url_blocked`` → 422 at the API).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from app.connectors.base import ConnectorError
from app.connectors.web.fetch import (
    UrlBlockedError,
    _is_blocked_ip,
    _validate_url,
    fetch_url,
    validate_url_syntactic,
)

# A public IP literal so the URL check passes the host stage without DNS — the
# example.com documentation range is never internal.
_PUBLIC_IP = "93.184.216.34"


# --- URL validation: blocked schemes + ranges (no network) ------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",  # IPv4 loopback
        "http://127.0.0.1:8080/admin",  # loopback with a port
        "http://10.0.0.5/x",  # RFC-1918 (10/8)
        "http://172.16.5.5/x",  # RFC-1918 (172.16/12)
        "http://192.168.1.1/x",  # RFC-1918 (192.168/16)
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://169.254.0.1/x",  # link-local generally
        "http://100.64.0.1/x",  # CGNAT 100.64/10
        "http://0.0.0.0/x",  # unspecified
        "http://[::1]/x",  # IPv6 loopback
        "http://[::ffff:127.0.0.1]/x",  # IPv4-mapped IPv6 loopback
        "http://[fc00::1]/x",  # IPv6 unique-local
        "http://[fe80::1]/x",  # IPv6 link-local
    ],
)
def test_blocked_ip_literals_are_refused(url: str) -> None:
    """An IP-literal host in any blocked range is refused (url_blocked)."""
    with pytest.raises(UrlBlockedError) as exc:
        _validate_url(url)
    assert exc.value.code == "url_blocked"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "gopher://example.com/x",
        "data:text/plain;base64,SGVsbG8=",
        "//example.com/x",  # scheme-relative → no scheme
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    """Only http/https are fetched; every other scheme is refused before any I/O."""
    with pytest.raises(UrlBlockedError):
        _validate_url(url)


def test_url_without_host_is_refused() -> None:
    with pytest.raises(UrlBlockedError):
        _validate_url("http:///nohost")


def test_public_ip_literal_passes_validation() -> None:
    """A public IP literal passes the host stage (the positive control)."""
    safe_ip, host, port = _validate_url(f"http://{_PUBLIC_IP}/page")
    assert safe_ip == _PUBLIC_IP
    assert host == _PUBLIC_IP
    assert port == "80"


# --- Bounded, non-blocking request-path pre-check (no DNS) -------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",  # IPv4 loopback literal
        "http://169.254.169.254/latest/meta-data/",  # metadata link-local literal
        "http://10.0.0.5/x",  # RFC-1918 literal
        "http://[::1]/x",  # IPv6 loopback literal
        "ftp://example.com/x",  # bad scheme
        "http:///nohost",  # no host
    ],
)
def test_validate_url_syntactic_refuses_literals_and_schemes(url: str) -> None:
    """The bounded pre-check refuses IP-literal SSRF + bad schemes synchronously."""
    with pytest.raises(UrlBlockedError):
        validate_url_syntactic(url)


def test_validate_url_syntactic_does_no_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (review finding 3): the pre-check never calls ``getaddrinfo``.

    A *hostname* (not an IP literal) is admitted by the bounded check without any
    resolution — DNS is deferred to the fetch path so the request thread never
    blocks on ``socket.getaddrinfo`` (ADR-0009 §3).
    """
    import socket

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("validate_url_syntactic must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    host = validate_url_syntactic("https://example.com/path")
    assert host == "example.com"


def test_validate_url_does_dns_at_fetch_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The authoritative fetch-time guard still resolves the host (DNS, off-request).

    Confirms the split keeps DNS on the fetch path: ``_validate_url`` (the
    fetch-time guard) resolves a hostname and rejects it when it maps to a blocked
    address — here a stubbed ``getaddrinfo`` returns loopback for a public-looking
    host (the DNS-rebind class), which must be refused.
    """
    import socket

    def _resolve_loopback(*_args: object, **_kwargs: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _resolve_loopback)
    with pytest.raises(UrlBlockedError):
        _validate_url("https://rebind.example.com/x")


@pytest.mark.parametrize(
    "addr,blocked",
    [
        ("127.0.0.1", True),
        ("10.1.2.3", True),
        ("192.168.0.1", True),
        ("169.254.169.254", True),
        ("100.100.100.100", True),  # CGNAT
        ("::1", True),
        ("fc00::1", True),
        ("::ffff:127.0.0.1", True),  # mapped loopback
        ("8.8.8.8", False),
        ("93.184.216.34", False),
        ("2606:2800:220:1:248:1893:25c8:1946", False),  # public IPv6
    ],
)
def test_is_blocked_ip_classification(addr: str, blocked: bool) -> None:
    """The IP-range classifier flags exactly the SSRF-dangerous ranges."""
    import ipaddress

    assert _is_blocked_ip(ipaddress.ip_address(addr)) is blocked


# --- Fetch path (MockTransport, fully offline) ------------------------------


def _ok_html(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><head><title>Doc</title></head><body><p>Body text.</p></body></html>",
    )


async def _fetch(handler: object, url: str = f"http://{_PUBLIC_IP}/page", **kwargs: object):
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        return await fetch_url(url, client=client, **kwargs)  # type: ignore[arg-type]


async def test_fetch_returns_text_and_final_url() -> None:
    result = await _fetch(_ok_html)
    assert result.content_type == "text/html"
    assert "Body text." in result.text
    assert result.final_url == f"http://{_PUBLIC_IP}/page"


async def test_redirect_to_loopback_is_refused() -> None:
    """A public host that 30x-redirects to loopback is refused on the next hop."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    with pytest.raises(UrlBlockedError):
        await _fetch(handler)


async def test_redirect_to_metadata_is_refused() -> None:
    """The classic SSRF pivot: redirect to the cloud-metadata IP is refused."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(UrlBlockedError):
        await _fetch(handler)


async def test_redirect_to_private_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://10.0.0.9/internal"})

    with pytest.raises(UrlBlockedError):
        await _fetch(handler)


async def test_redirect_chain_to_public_is_followed() -> None:
    """A public→public redirect IS followed and re-validated (the positive case)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # First hop pins to the public IP and redirects to another public IP.
            return httpx.Response(302, headers={"location": "http://8.8.8.8/final"})
        return _ok_html(request)

    result = await _fetch(handler)
    assert result.final_url == "http://8.8.8.8/final"
    assert "Body text." in result.text


async def test_too_many_redirects_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # Always redirect to another public IP → exceeds the hop cap.
        return httpx.Response(302, headers={"location": "http://8.8.8.8/loop"})

    with pytest.raises(UrlBlockedError):
        await _fetch(handler, max_redirects=2)


async def test_disallowed_content_type_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01"
        )

    with pytest.raises(UrlBlockedError):
        await _fetch(handler)


async def test_oversize_response_is_refused() -> None:
    """The size cap is enforced on the streamed bytes, not the header."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 4096)

    with pytest.raises(UrlBlockedError):
        await _fetch(handler, max_bytes=1024)


async def test_transport_error_maps_to_connector_error() -> None:
    """A transport fault is a ConnectorError (not a leaked httpx error)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(ConnectorError) as exc:
        await _fetch(handler)
    assert exc.value.code == "fetch_failed"


async def test_redirect_without_location_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)  # no Location header

    with pytest.raises(UrlBlockedError):
        await _fetch(handler)


# --- Non-2xx status gate + outbound User-Agent (regression: issue #138) ------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 429, 500, 502, 503])
async def test_non_2xx_final_response_is_a_fetch_failure(status: int) -> None:
    """A non-2xx final response is a fetch fault, never ingestible content (#138).

    Wikimedia serves a 403 *HTML* page to UA-less requests; without a status gate
    that error page was decoded and returned as ``FetchResult.text`` and indexed
    as if it were the article. The chokepoint must instead raise
    ``ConnectorError(code="fetch_failed")`` so the sync is marked failed and the
    error body is never ingested.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body><p>Please set a user-agent.</p></body></html>",
        )

    with pytest.raises(ConnectorError) as exc:
        await _fetch(handler)
    assert exc.value.code == "fetch_failed"


@pytest.mark.parametrize("status", [200, 201, 203])
async def test_2xx_response_is_returned_as_content(status: int) -> None:
    """A 2xx response with a real body is still ingested (the status gate is not over-broad)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers={"content-type": "text/html"}, text="<p>Real body.</p>"
        )

    result = await _fetch(handler)
    assert "Real body." in result.text


async def test_outbound_request_sends_configured_user_agent() -> None:
    """The fetch carries the supplied descriptive User-Agent on every hop (#138)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return _ok_html(request)

    await _fetch(handler, user_agent="LumenCopilot/9.9 (+https://example.test/bot)")
    assert seen["ua"] == "LumenCopilot/9.9 (+https://example.test/bot)"


async def test_outbound_request_defaults_to_descriptive_user_agent() -> None:
    """With no explicit UA the chokepoint still sends a descriptive (non-httpx) UA."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return _ok_html(request)

    await _fetch(handler)
    assert "LumenCopilot" in seen["ua"]
    assert "python-httpx" not in seen["ua"].lower()


# --- DNS resolution must not run on the event loop (#524) --------------------
#
# ``_validate_url`` resolves through the blocking ``socket.getaddrinfo``. Awaited
# inline, that parks the serving loop for the whole lookup, once per redirect hop
# — and, because it happens before the coroutine's first await, it also serialises
# concurrent fetches: gathering N pages produced overlapping HTTP but strictly
# sequential DNS. The web-search fetch leg (``search_web/service.py``) calls
# ``fetch_url`` from the chat runtime, so this is a request-path defect, not a
# worker-only one.
#
# These drive the WHOLE REAL GUARD — ``fetch_url`` → ``_validate_url`` →
# ``_resolve_safe_ip`` → ``resolve_safe_ip`` → ``socket.getaddrinfo`` — and stub
# only that last blocking primitive. A probe that replaced the chokepoint itself
# could not see this defect at all, which is precisely how it was missed.

_DNS_STALL_SECONDS = 0.25
_DNS_TICK_SECONDS = 0.01
# Resolution on the loop yields a gap of >= _DNS_STALL_SECONDS; off the loop the
# gaps stay at tick resolution.
_DNS_MAX_GAP_SECONDS = 0.1


class _ResolverProbe:
    """A blocking ``getaddrinfo`` stand-in that records thread and concurrency."""

    def __init__(self) -> None:
        self.threads: set[int] = set()
        self.in_flight = 0
        self.max_in_flight = 0

    def __call__(self, *_args: object, **_kwargs: object) -> list:
        import socket
        import threading

        self.threads.add(threading.get_ident())
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(_DNS_STALL_SECONDS)
        finally:
            self.in_flight -= 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 0))]


async def test_fetch_url_resolves_dns_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocking resolve happens on a worker thread, never the loop's own."""
    import socket
    import threading

    probe = _ResolverProbe()
    monkeypatch.setattr(socket, "getaddrinfo", probe)

    result = await _fetch(_ok_html, url="http://dns-probe.example.com/page")

    assert result.final_url == "http://dns-probe.example.com/page"
    assert probe.threads and threading.get_ident() not in probe.threads


async def test_fetch_url_does_not_stall_the_event_loop_during_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop keeps servicing other work while a host is being resolved."""
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", _ResolverProbe())

    gaps: list[float] = []

    async def _ticker() -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(_DNS_TICK_SECONDS)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    try:
        await asyncio.sleep(0)
        await _fetch(_ok_html, url="http://dns-probe.example.com/page")
    finally:
        ticker.cancel()

    # No tick at all means the loop never came back — the worst case, not a pass.
    assert max(gaps, default=float("inf")) < _DNS_MAX_GAP_SECONDS


async def test_concurrent_fetches_overlap_their_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gathered fetches resolve together, so the leg is not sum-of-DNS.

    This is the assertion that makes the #513 concurrency claim true end to end:
    with resolution inline, three gathered fetches resolved one after another and
    the peak stayed at 1 however they were gathered.
    """
    import socket

    probe = _ResolverProbe()
    monkeypatch.setattr(socket, "getaddrinfo", probe)

    urls = [f"http://host-{i}.example.com/page" for i in range(3)]
    results = await asyncio.gather(*(_fetch(_ok_html, url=u) for u in urls))

    assert [r.final_url for r in results] == urls
    assert probe.max_in_flight == 3
