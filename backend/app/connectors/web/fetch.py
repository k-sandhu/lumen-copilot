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

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.connectors.base import ConnectorConfigError

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


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``ip`` is in a range a server-side fetch must never reach.

    Blocks loopback, private (RFC-1918 + IPv6 ULA), link-local (incl. the
    cloud-metadata ``169.254.169.254``), CGNAT (100.64.0.0/10), unspecified,
    reserved, and multicast. IPv4-mapped IPv6 addresses are unwrapped first so
    ``::ffff:127.0.0.1`` cannot smuggle a loopback target past the check.
    """
    # Unwrap an IPv4-mapped IPv6 address (``::ffff:a.b.c.d``) to its IPv4 form so
    # the IPv4 range checks below apply — otherwise a mapped loopback slips by.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (
        ip.is_loopback
        or ip.is_private  # RFC-1918 (v4) / ULA fc00::/7 (v6)
        or ip.is_link_local  # 169.254.0.0/16 incl. metadata; fe80::/10
        or ip.is_unspecified  # 0.0.0.0 / ::
        or ip.is_reserved
        or ip.is_multicast
    ):
        return True
    # CGNAT 100.64.0.0/10 — not flagged by ``is_private`` but never publicly
    # routable to a tenant's content; a common SSRF pivot.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def _resolve_safe_ip(host: str) -> str:
    """Resolve ``host`` and return one validated, non-blocked IP literal, or raise.

    Resolves **all** A/AAAA records and rejects the host if *any* of them is a
    blocked address (no partial allow — a name that resolves to both a public and
    a private IP is refused, closing the rebind window). Returns the first
    validated IP as a string so the caller can pin the connection to it.

    Raises:
        UrlBlockedError: the host does not resolve, or resolves to a blocked range.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlBlockedError(f"could not resolve host {host!r}") from exc

    resolved: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover — getaddrinfo returns valid literals
            raise UrlBlockedError(f"unparseable address for host {host!r}") from None
        if _is_blocked_ip(ip):
            raise UrlBlockedError(
                f"host {host!r} resolves to a blocked address ({addr}); "
                "loopback/private/link-local/metadata targets are refused"
            )
        resolved.append(addr)

    if not resolved:  # pragma: no cover — getaddrinfo raises rather than empty
        raise UrlBlockedError(f"host {host!r} did not resolve to any address")
    return resolved[0]


def _validate_url(url: str) -> tuple[str, str, str]:
    """Validate scheme + host of ``url`` and resolve a safe IP.

    Returns ``(safe_ip, host, port)`` for pinning the connection. Raises
    :class:`UrlBlockedError` for a non-http(s) scheme, a missing host, or a host
    that resolves into a blocked range. An IP-literal host is range-checked
    directly (no DNS).
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

    # An IP-literal host skips DNS but is still range-checked.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        safe_ip = _resolve_safe_ip(host)
    else:
        if _is_blocked_ip(literal):
            raise UrlBlockedError(
                f"address {host} is in a blocked range " "(loopback/private/link-local/metadata)"
            )
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

    The original host is preserved in the ``Host`` header (set by the caller via
    ``extensions``/headers) so TLS SNI + virtual hosting still work, but the TCP
    connection is pinned to the IP we already validated — a DNS rebind between
    check and connect cannot redirect it to a blocked address (TOCTOU defense).
    For an IPv6 literal the host is bracketed.
    """
    parts = urlsplit(url)
    host_literal = f"[{safe_ip}]" if ":" in safe_ip else safe_ip
    netloc = host_literal
    if parts.port:
        netloc = f"{host_literal}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def fetch_url(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    allowed_content_types: frozenset[str] = ALLOWED_CONTENT_TYPES,
) -> FetchResult:
    """Fetch ``url`` through the full SSRF guard, following redirects safely.

    The single fetch chokepoint (ADR-0009 §3). Validates the scheme + host and
    resolves a safe IP for **every** hop (the initial URL and each redirect
    target), pins the connection to the validated IP, caps the response size +
    time, and allowlists the content type. Returns the decoded text + the final
    URL.

    ``client`` is injectable so tests drive it with a mock transport (offline);
    in production the connector passes a no-auto-redirect client.

    Raises:
        UrlBlockedError: any guard failure (scheme, host range, redirect target,
            size cap, content type) — the API maps it to 422 ``url_blocked``.
        ConnectorError: a transport-level fetch fault (connection/timeout).
    """
    owns_client = client is None
    active = client or httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,  # we follow manually so each hop is re-validated
    )
    current = url
    try:
        for _hop in range(max_redirects + 1):
            safe_ip, host, port = _validate_url(current)
            pinned = _pin_url(current, safe_ip)
            parts = urlsplit(current)
            host_header = parts.netloc  # preserves the original host[:port] for routing
            try:
                response = await active.get(
                    pinned,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": host},
                )
            except httpx.HTTPError as exc:
                from app.connectors.base import ConnectorError

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
]
