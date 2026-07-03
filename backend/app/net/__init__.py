"""Shared low-level networking primitives (no adapter/vendor coupling).

Currently the **SSRF egress guard** (:mod:`app.net.egress`) — the one canonical
predicate for "is this address a target a server-side connection must never
reach" plus the connection-pinning helper. It was extracted from
``connectors/web/fetch.py`` so the web connector and the MCP adapter
(``app/mcp/``) share **one** SSRF definition rather than two drifting copies
(ADR-0009 §3 / ADR-0012 §4 — "one place to get SSRF right, one place with the
negative tests").
"""
