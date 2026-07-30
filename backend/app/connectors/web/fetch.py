"""SSRF-guarded HTTP fetch — the one chokepoint (ADR-0009 §3, risk:security).

**Load-bearing.** The server fetches *user-supplied* URLs, so every fetch passes
through here and nowhere else. A bypass is a blocking defect (ADR-0009 §3). The
guard, in order:

* **scheme** — allow only ``http`` / ``https`` (no ``file:``, ``ftp:``,
  ``gopher:``, ``data:`` …);
* **host resolution** — resolve the host to its IP(s) and **reject** any that is
  loopback, private (RFC-1918), link-local, CGNAT (100.64.0.0/10), unique-local
  IPv6, or the cloud-metadata address (``169.254.169.254``). A hostname that
  resolves to *any* blocked address is rejected (no partial allow);
* **redirects** — followed **manually**, re-validating the target of **every**
  hop the same way, so a public URL that 30x-redirects to ``127.0.0.1`` or the
  metadata IP is refused (never followed to a blocked target);
* **limits** — a wall-clock timeout, a redirect-hop cap, a response **size** cap
  (enforced while streaming, so an over-cap body is aborted, not buffered), and a
  **content-type allowlist** (only ``text/*`` and the feed/sitemap XML types).

Every rejection raises :class:`~app.connectors.base.ConnectorConfigError` with a
stable ``code`` (``url_blocked`` for an SSRF rejection) which the API maps to
**422** (ADR-0009 §3). The actual socket connection is **pinned to the validated
IP** (``Host`` header preserved) so a DNS-rebind between check and connect cannot
swap in a blocked address (TOCTOU defense).
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.connectors.base import ConnectorConfigError, ConnectorError
from app.net.egress import (
    EgressBlockedError,
    is_blocked_ip,
    pin_url_to_ip,
    resolve_safe_ip,
)

# Schemes a public fetch may use. Everything else (file/ftp/gopher/data/…) is a
# hard reject before any network work (ADR-0009 §3).
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Content types the web connector will ingest: readable text + the feed/sitemap
# XML carriers. A response whose declared type is outside this set is refused
# (ADR-0009 §3 "allowlist content types").
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/xml",
        "application/xml",
        "application/rss+xml",
        "application/atom+xml",
        "application/xhtml+xml",
    }
)

# Defaults (overridable by the connector for tests/config). Conservative so a
# hostile or pathological target cannot exhaust the worker.
_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB per fetched resource
_DEFAULT_MAX_REDIRECTS = 5

# Descriptive fallback User-Agent. Many sites (e.g. Wikimedia) reject a request
# that announces no descriptive client with a 4xx error *page*; sending a real UA
# keeps the fetcher a well-behaved client (ADR-0009 §3, issue #138). The connector
# overrides this with the configured ``settings.web_user_agent``; this constant is
# the fallback for a direct/standalone call so the chokepoint is never UA-less.
_DEFAULT_USER_AGENT = "LumenCopilot (+https://github.com/k-sandhu/lumen-copilot)"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """The validated bytes of one fetched resource (already SSRF-checked).

    ``final_url`` is the last URL in the (re-validated) redirect chain — the
    canonical location to record for provenance.
    """

    url: str
    final_url: str
    content_type: str
    text: str


class UrlBlockedError(ConnectorConfigError):
    """A URL was refused by the SSRF guard (→ 422 ``url_blocked``).

    Raised for every guard failure: a non-http(s) scheme, a missing host, a host
    that resolves to a blocked range, or a redirect to a blocked target. The
    ``code`` is always ``url_blocked`` so the API surfaces one stable Problem code
    regardless of *which* check tripped (the detail explains).
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail, code="url_blocked")


# The SSRF range predicate + resolver now live in the shared ``app.net.egress``
# primitive so the web fetcher and the MCP adapter (``app/mcp/``) share ONE
# definition (ADR-0009 §3 / ADR-0012 §4). These thin aliases keep this module's
# established internal names/behaviour (and its negative tests) unchanged while
# the implementation is single-sourced.
def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``ip`` is in a range a server-side fetch must never reach.

    Delegates to :func:`app.net.egress.is_blocked_ip` — the one canonical
    predicate (loopback / private / link-local incl. metadata / CGNAT /
    unspecified / reserved / multicast, IPv4-mapped IPv6 unwrapped first).
    """
    return is_blocked_ip(ip)


def _resolve_safe_ip(host: str) -> str:
    """Resolve ``host`` to one validated non-blocked IP literal, or raise (→ 422).

    Delegates to :func:`app.net.egress.resolve_safe_ip` (resolve-all, reject-any)
    and re-raises its :class:`~app.net.egress.EgressBlockedError` as this module's
    :class:`UrlBlockedError` so the API still surfaces the stable ``url_blocked``
    Problem code.

    Raises:
        UrlBlockedError: the host does not resolve, or resolves to a blocked range.
    """
    try:
        return resolve_safe_ip(host)
    except EgressBlockedError as exc:
        raise UrlBlockedError(str(exc)) from exc


def validate_url_syntactic(url: str) -> str:
    """Bounded, **non-blocking** SSRF pre-check (no DNS) — safe on the request path.

    Validates the cheap, synchronous parts of the SSRF guard only: the scheme is
    ``http``/``https``, a host is present, and — if the host is an **IP literal** —
    its range is checked directly (loopback/private/link-local/metadata refused).
    A *hostname* is left for fetch-time resolution: this function does **no**
    ``getaddrinfo`` so it never blocks the event loop (ADR-0009 §3 — keep
    request-path validation bounded; the full per-redirect DNS guard runs in the
    Celery sync path). Returns the host on success.

    Raises:
        UrlBlockedError: a non-http(s) scheme, a missing host, or an IP-literal
            host in a blocked range.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlBlockedError(
            f"scheme {scheme or '(none)'!r} is not allowed; only http/https are fetched"
        )
    host = parts.hostname
    if not host:
        raise UrlBlockedError("URL has no host")

    # An IP-literal host is range-checked synchronously (no DNS). A hostname is
    # left for fetch-time resolution (off the event loop / in the worker).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return host
    if _is_blocked_ip(literal):
        raise UrlBlockedError(
            f"address {host} is in a blocked range " "(loopback/private/link-local/metadata)"
        )
    return host


def _validate_url(url: str) -> tuple[str, str, str]:
    """Validate scheme + host of ``url`` and resolve a safe IP (fetch-time, DNS).

    Returns ``(safe_ip, host, port)`` for pinning the connection. Runs the
    bounded syntactic checks (:func:`validate_url_syntactic`) and then — for a
    hostname — resolves it, rejecting the host if *any* resolved address is in a
    blocked range. An IP-literal host needs no DNS (already range-checked).
    Raises :class:`UrlBlockedError` on any guard failure.

    **Blocking** (``socket.getaddrinfo``): call it from a thread, never inline on
    an event loop. :func:`fetch_url` does that for you — it is the only caller,
    and it hops to a thread per redirect hop. Callers reach this guard from both
    the Celery connector sync *and* the request-path web-search fetch leg
    (``search_web/service.py``), so "it only runs in the worker" is not a safe
    assumption to build on (#524).
    """
    host = validate_url_syntactic(url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()

    # An IP-literal host skips DNS (already validated); a hostname is resolved.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        safe_ip = _resolve_safe_ip(host)
    else:
        safe_ip = host

    port = str(parts.port) if parts.port else ("443" if scheme == "https" else "80")
    return safe_ip, host, port


def _check_content_type(content_type: str, allowed: frozenset[str]) -> str:
    """Return the bare media type if allowlisted, else raise.

    The leading parameter of a ``Content-Type`` (``; charset=…``) is stripped
    before the allowlist check.
    """
    bare = content_type.split(";", 1)[0].strip().lower()
    if bare and bare not in allowed:
        raise UrlBlockedError(
            f"content-type {bare!r} is not allowed; only text/* and feed/sitemap XML are ingested"
        )
    return bare


async def _read_capped(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Stream the body, aborting once it exceeds ``max_bytes`` (no full buffer).

    A hostile target can advertise a small ``Content-Length`` and stream more, so
    the cap is enforced on the bytes actually read, not the header.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise UrlBlockedError(f"response exceeds the {max_bytes}-byte size cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _pin_url(url: str, safe_ip: str) -> str:
    """Rewrite ``url``'s host to the validated IP so the socket connects to it.

    Delegates to :func:`app.net.egress.pin_url_to_ip` — the shared IP-pinning
    rewrite (TOCTOU defence). The original host is preserved in the ``Host``
    header + TLS SNI (set by the caller) so virtual hosting still works.
    """
    return pin_url_to_ip(url, safe_ip)


async def fetch_url(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    allowed_content_types: frozenset[str] = ALLOWED_CONTENT_TYPES,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> FetchResult:
    """Fetch ``url`` through the full SSRF guard, following redirects safely.

    The single fetch chokepoint (ADR-0009 §3). Validates the scheme + host and
    resolves a safe IP for **every** hop (the initial URL and each redirect
    target), pins the connection to the validated IP, caps the response size +
    time, requires a **2xx** final status, and allowlists the content type.
    Returns the decoded text + the final URL.

    ``client`` is injectable so tests drive it with a mock transport (offline);
    in production the connector passes a no-auto-redirect client. ``user_agent``
    is sent on **every** hop (the connector passes ``settings.web_user_agent``):
    a UA-less request is rejected by many sites with an error *page* that would
    otherwise be ingested as content (issue #138).

    Raises:
        UrlBlockedError: any SSRF/guard failure (scheme, host range, redirect
            target, size cap, content type) — the API maps it to 422 ``url_blocked``.
        ConnectorError: a transport-level fetch fault (connection/timeout) **or a
            non-2xx final response** (``code="fetch_failed"``) — a failed fetch,
            never ingestible content.
    """
    owns_client = client is None
    active = client or httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,  # we follow manually so each hop is re-validated
    )
    current = url
    try:
        for _hop in range(max_redirects + 1):
            # Off the event loop: _validate_url resolves DNS through the blocking
            # socket.getaddrinfo (net/egress.py), and this coroutine would
            # otherwise run it inline — parking the loop for the whole lookup,
            # once per redirect hop. That also serialised concurrent fetches
            # before their first await, so callers that gather several pages got
            # overlapping HTTP but strictly sequential DNS (#513 review, #524).
            # DNS is blocking *I/O*, so a thread is the right home for it; the
            # guard itself is unchanged — same resolve-all / reject-any / pinning,
            # still re-run on every hop.
            safe_ip, host, port = await asyncio.to_thread(_validate_url, current)
            pinned = _pin_url(current, safe_ip)
            parts = urlsplit(current)
            host_header = parts.netloc  # preserves the original host[:port] for routing
            try:
                response = await active.get(
                    pinned,
                    headers={"Host": host_header, "User-Agent": user_agent},
                    extensions={"sni_hostname": host},
                )
            except httpx.HTTPError as exc:
                raise ConnectorError(
                    f"could not fetch {current!r}: {type(exc).__name__}", code="fetch_failed"
                ) from exc

            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise UrlBlockedError(f"redirect from {current!r} had no Location")
                # Resolve the (possibly relative) Location against the current URL,
                # then loop — the next iteration re-validates the target (so a
                # redirect to a blocked address is refused, never followed).
                current = str(httpx.URL(current).join(location))
                continue

            # A non-2xx final response is a *failed fetch*, not content: a 403/404/
            # 500 error page (e.g. Wikimedia's UA-gate 403) must never be decoded
            # and ingested as the document. Surface it as ``fetch_failed`` so the
            # sync is marked failed (last_error) rather than indexing the rejection
            # (issue #138). Checked before content-type/body so the error body is
            # never read.
            if not response.is_success:
                status_code = response.status_code
                await response.aclose()
                raise ConnectorError(
                    f"fetch of {current!r} returned HTTP {status_code}", code="fetch_failed"
                )

            content_type = response.headers.get("content-type", "")
            try:
                bare_type = _check_content_type(content_type, allowed_content_types)
                body = await _read_capped(response, max_bytes=max_bytes)
            finally:
                await response.aclose()
            text = body.decode(response.encoding or "utf-8", errors="replace")
            return FetchResult(
                url=url,
                final_url=current,
                content_type=bare_type,
                text=text,
            )
        raise UrlBlockedError(f"too many redirects (> {max_redirects}) starting from {url!r}")
    finally:
        if owns_client:
            await active.aclose()


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "FetchResult",
    "UrlBlockedError",
    "fetch_url",
    "validate_url_syntactic",
]
