"""Web-search adapter — the only issuer of an internet-search query (ADR-0014 §2).

Single responsibility (AGENTS.md §6 new boundary row, ADR-0014): own the
web-search provider client (self-hosted **SearXNG** today; a hosted API
swappable behind this same seam) and return the domain
:class:`~app.domain.web_search.WebSearchResult` — **no vendor/response type leaks
upward** (ADR-0004 adapter rule 1). Deliberately separate from
``app/search/`` (the tenant's own indexed corpus) and ``app/connectors/web/``
(ingestion): web search returns transient results the model reads live, it does
not ingest.

Two outbound legs (ADR-0014 §3):

* the **query leg** (adapter -> SearXNG) — a trusted internal hop, time-bounded;
* the **result-page fetch leg** — the untrusted leg, funnelled through the one
  ``connectors/web/fetch.py`` SSRF chokepoint (https-only, blocked-range checks
  on every redirect hop, size/time caps, User-Agent). No second egress path.

Every search is admission-checked against the per-tenant Redis fixed-window rate
limiter (``tasks/rate_limit.py``) so one tenant cannot fan out unbounded outbound
requests (a DoS/amplification pivot).
"""

from __future__ import annotations

from app.search_web.client import (
    SearxngClient,
    WebSearchClient,
    WebSearchError,
    WebSearchRateLimited,
    WebSearchUnavailable,
)
from app.search_web.service import WebSearchService, build_web_search_service

__all__ = [
    "SearxngClient",
    "WebSearchClient",
    "WebSearchError",
    "WebSearchRateLimited",
    "WebSearchService",
    "WebSearchUnavailable",
    "build_web_search_service",
]
