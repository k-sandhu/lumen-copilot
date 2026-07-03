"""SSRF-guarded egress for the MCP client — the load-bearing control (risk:security).

A remote MCP server is a **user-supplied endpoint the server connects to**, so —
exactly like ``connectors/web/fetch.py`` — every outbound MCP connection must pass
the SSRF discipline (ADR-0012 §4). This module puts that guard **on the path the
SDK actually uses**: it builds an :class:`httpx.AsyncClient` whose transport
validates and IP-pins *every* request, and — because httpx re-invokes the
transport once per redirect hop — *every redirect target* too. The MCP SDK owns
its redirect handling; injecting a guarding transport (rather than a one-shot
pre-check) is the only way the guard cannot be bypassed by an SDK-followed
redirect to a blocked address. If a transport could not be guarded this way it
would not ship (ADR-0012 §4).

The guard, per hop:

* **https-only** — a non-``https`` request URL is refused (stricter than the web
  connector: MCP carries credentials);
* **resolve + range-check** — the host is resolved and rejected if it (or any of
  its addresses) is loopback / private / link-local / CGNAT / cloud-metadata
  (the shared :mod:`app.net.egress` predicate — one SSRF definition);
* **IP-pin** — the socket connects to the *validated* IP with the original host
  preserved in the ``Host`` header + TLS SNI, closing the DNS-rebind (TOCTOU)
  window.

A rejection raises :class:`McpEgressBlockedError`; the client adapter turns it
into a contained ``ok=False`` result (never an exception out of ``app/mcp/``).
"""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from app.net.egress import EgressBlockedError, pin_url_to_ip, resolve_safe_ip

# MCP endpoints carry credentials, so — unlike the web connector's http/https —
# only https is accepted for a remote server (ADR-0012 §4).
_ALLOWED_SCHEME = "https"


class McpEgressError(Exception):
    """Base class for an MCP egress failure (kept inside ``app/mcp/``)."""


class McpEgressBlockedError(McpEgressError):
    """An MCP outbound target was refused by the SSRF guard (blocked/non-https).

    Raised on connect **or** any redirect hop for a non-https scheme, a missing
    host, or a host resolving to a blocked range. The client adapter maps it to a
    contained ``ok=False`` ``McpToolResult`` (``mcp_egress_blocked``); it never
    escapes the module.
    """


class _SsrfGuardTransport(httpx.AsyncBaseTransport):
    """An httpx transport that SSRF-validates + IP-pins **every** request it sees.

    httpx invokes ``handle_async_request`` once per hop, including each redirect
    target the client follows, so validating here covers the initial connect and
    *every* redirect — a public URL that 30x-redirects to ``127.0.0.1`` or the
    metadata IP is refused at the hop, not followed (ADR-0012 §4). Delegates the
    actual I/O to an inner :class:`httpx.AsyncHTTPTransport` after rewriting the
    request to the validated IP (Host header + SNI preserved).
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme != _ALLOWED_SCHEME:
            raise McpEgressBlockedError(
                f"scheme {scheme or '(none)'!r} is not allowed for an MCP server; "
                "only https is permitted (credentials in transit)"
            )
        host = parts.hostname
        if not host:
            raise McpEgressBlockedError("MCP endpoint URL has no host")

        # Resolve-all / reject-any + range check (shared predicate). A blocked
        # target — on this hop or a redirect that landed here — is refused.
        try:
            safe_ip = resolve_safe_ip(host)
        except EgressBlockedError as exc:
            raise McpEgressBlockedError(str(exc)) from exc

        # Pin the connection to the validated IP; preserve the original host for
        # routing (Host header) and TLS (SNI) so virtual hosting/TLS still work.
        pinned = pin_url_to_ip(url, safe_ip)
        host_header = parts.netloc  # original host[:port] for routing
        pinned_request = httpx.Request(
            method=request.method,
            url=pinned,
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        pinned_request.headers["Host"] = host_header
        return await self._inner.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_guarded_client(
    *,
    headers: dict[str, str] | None,
    timeout: httpx.Timeout | None,
    auth: httpx.Auth | None,
    default_timeout_seconds: float,
    user_agent: str,
    inner_transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Build the SSRF-guarded :class:`httpx.AsyncClient` the SDK transports use.

    Signature-compatible with the MCP SDK's ``McpHttpClientFactory`` (``headers`` /
    ``timeout`` / ``auth``) so it can be passed straight in as
    ``httpx_client_factory``; the extra keyword-only params (default timeout, UA,
    optional inner transport) are bound by the client adapter via a partial. The
    returned client:

    * follows redirects (as the SDK expects) but routes **every** hop through the
      :class:`_SsrfGuardTransport`, so each redirect target is re-validated + pinned;
    * sends a descriptive, version-stamped ``User-Agent`` on every request;
    * is bounded by the configured wall-clock timeout.

    ``inner_transport`` swaps the real :class:`httpx.AsyncHTTPTransport` (production)
    for a test double (e.g. :class:`httpx.ASGITransport` mounting an in-process
    fixture MCP server) — the SSRF guard still wraps it, so a test exercises the
    **real** guard fully offline. Production passes ``None``.
    """
    merged_headers = {"User-Agent": user_agent}
    if headers:
        merged_headers.update(headers)
    return httpx.AsyncClient(
        transport=_SsrfGuardTransport(inner_transport or httpx.AsyncHTTPTransport()),
        follow_redirects=True,
        timeout=timeout if timeout is not None else httpx.Timeout(default_timeout_seconds),
        headers=merged_headers,
        auth=auth,
    )


__all__ = [
    "McpEgressBlockedError",
    "McpEgressError",
    "build_guarded_client",
]
