"""The canonical SSRF egress guard — one predicate, reused everywhere (risk:security).

**Load-bearing.** Whenever the server opens an outbound connection to a
*user-supplied* endpoint (a web-connector URL, a remote MCP server), the host it
resolves to must be checked against the set of address ranges a server-side
request must never reach. This module owns that predicate — the range check, the
"resolve-all-and-reject-any" resolver, and the IP-pinning rewrite — so every
egress path shares **one** definition and **one** set of negative tests (ADR-0009
§3, ADR-0012 §4). ``connectors/web/fetch.py`` and ``app/mcp/`` both call these
helpers rather than re-deriving them; a divergent second copy is exactly the
failure this extraction prevents.

The guard, in order:

* **address range** — reject loopback, private (RFC-1918 / IPv6 ULA), link-local
  (incl. the cloud-metadata ``169.254.169.254``), CGNAT (100.64.0.0/10),
  unspecified, reserved, and multicast. IPv4-mapped IPv6 is unwrapped first so
  ``::ffff:127.0.0.1`` cannot smuggle a loopback target past the check;
* **resolve-all, reject-any** — resolve every A/AAAA record and reject the host
  if *any* resolves to a blocked range (no partial allow — closes the split-DNS
  window);
* **pin** — the caller connects to the *validated IP* (``Host``/SNI preserved) so
  a DNS rebind between check and connect cannot swap in a blocked address (TOCTOU
  defence).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


class EgressBlockedError(Exception):
    """An outbound target was refused by the SSRF guard (a blocked address/host).

    Raised for a host that does not resolve or resolves to a blocked range. Each
    egress caller wraps this in its own typed error (the web fetcher's
    ``UrlBlockedError`` → 422; the MCP adapter's ``McpEgressBlockedError`` → an
    ``ok=False`` tool result) so the low-level predicate stays free of any one
    caller's error taxonomy.
    """


# The CGNAT shared-address space (RFC-6598). Not flagged by ``is_private`` but
# never a publicly-routable tenant target — a common SSRF pivot.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``ip`` is in a range a server-side connection must never reach.

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
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_V4:
        return True
    return False


def check_ip_literal(host: str) -> None:
    """If ``host`` is an IP literal in a blocked range, raise; a hostname is a no-op.

    The synchronous, DNS-free half of the guard: an IP-literal host is range-
    checked immediately; a *name* is left for :func:`resolve_safe_ip` at connect
    time. Safe to call on any code path (no ``getaddrinfo``, never blocks).

    Raises:
        EgressBlockedError: ``host`` is an IP literal in a blocked range.
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return  # a hostname — resolved later
    if is_blocked_ip(literal):
        raise EgressBlockedError(
            f"address {host} is in a blocked range (loopback/private/link-local/metadata)"
        )


def resolve_safe_ip(host: str) -> str:
    """Resolve ``host`` and return one validated, non-blocked IP literal, or raise.

    Resolves **all** A/AAAA records and rejects the host if *any* of them is a
    blocked address (no partial allow — a name that resolves to both a public and
    a private IP is refused, closing the rebind window). An IP-literal host is
    returned as-is after a range check. Returns the first validated IP so the
    caller can pin the connection to it.

    Raises:
        EgressBlockedError: the host does not resolve, or resolves to a blocked
            range (or is a blocked IP literal).
    """
    # An IP literal needs no DNS — range-check it and return it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if is_blocked_ip(literal):
            raise EgressBlockedError(
                f"address {host} is in a blocked range (loopback/private/link-local/metadata)"
            )
        return host

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise EgressBlockedError(f"could not resolve host {host!r}") from exc

    resolved: list[str] = []
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover — getaddrinfo returns valid literals
            raise EgressBlockedError(f"unparseable address for host {host!r}") from None
        if is_blocked_ip(ip):
            raise EgressBlockedError(
                f"host {host!r} resolves to a blocked address ({addr}); "
                "loopback/private/link-local/metadata targets are refused"
            )
        resolved.append(addr)

    if not resolved:  # pragma: no cover — getaddrinfo raises rather than empty
        raise EgressBlockedError(f"host {host!r} did not resolve to any address")
    return resolved[0]


def pin_url_to_ip(url: str, safe_ip: str) -> str:
    """Rewrite ``url``'s host to the validated IP so the socket connects to it.

    The original host must be preserved in the ``Host`` header and the TLS SNI
    (the caller supplies those) so virtual hosting + TLS still work, but the TCP
    connection is pinned to the IP we already validated — a DNS rebind between
    check and connect cannot redirect it to a blocked address (TOCTOU defence).
    For an IPv6 literal the host is bracketed.
    """
    parts = urlsplit(url)
    host_literal = f"[{safe_ip}]" if ":" in safe_ip else safe_ip
    netloc = host_literal
    if parts.port:
        netloc = f"{host_literal}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


__all__ = [
    "EgressBlockedError",
    "check_ip_literal",
    "is_blocked_ip",
    "pin_url_to_ip",
    "resolve_safe_ip",
]
