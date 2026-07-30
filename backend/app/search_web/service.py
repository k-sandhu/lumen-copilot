"""Web-search orchestration — rate-limit, query, and cite-worthy page fetch (ADR-0014).

The ``search_web/`` use-case the ``web_search`` tool calls. It composes three
collaborators and leaks none of them upward (returns only the domain
:class:`~app.domain.web_search.WebSearchResult`):

1. the per-tenant **rate limiter** (``tasks/rate_limit.py``) — one admission check
   per search; an exhausted window raises :class:`WebSearchRateLimited` (ADR-0014
   §3), so one tenant cannot fan out unbounded outbound requests;
2. the provider **client** (SearXNG) — issues the query leg (a trusted internal
   hop) and maps the JSON to domain results;
3. the **SSRF chokepoint** (``connectors/web/fetch.py`` + ``extract.py``) — the
   *only* egress path for the untrusted result-page fetch leg, reused verbatim
   (https-only, blocked-range checks on every redirect hop, size/time caps,
   User-Agent). A result-page fetch that bypasses it would be a blocking defect
   (ADR-0014 §3); this module always calls ``fetch_url``.

Page fetching is **optional** (``fetch_top_n``): the top-N results are fetched +
extracted so the model can ground on passage-level text (INV-3 — only the fetched
passage is cite-worthy). A page that is SSRF-blocked, unreachable, or non-2xx is
**skipped** (its ``fetched_passage`` stays ``None``, the snippet still stands) —
one hostile result never fails the whole search.

Those top-N pages are fetched **concurrently** (#513). They are independent
requests to different untrusted hosts, so fetching them in sequence costs the sum
of the pages rather than the slowest one — and since these are arbitrary internet
hosts, slow and dead ones are normal: at the default per-fetch timeout, three
unreachable hosts serialize into ~30 s inside a live chat turn. ``fetch_top_n``
is itself the concurrency bound (a small configured cap), and every fetch still
goes through the one SSRF chokepoint.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import httpx

from app.connectors.base import ConnectorError
from app.connectors.web.extract import extract_page_text
from app.connectors.web.fetch import UrlBlockedError, fetch_url
from app.core.config import Settings
from app.domain.web_search import WebSearchResult
from app.search_web.client import (
    SearxngClient,
    WebSearchClient,
    WebSearchRateLimited,
)
from app.tasks.rate_limit import RateLimiter, RedisFixedWindowRateLimiter

# How much extracted passage text to keep per fetched page. Bounds the cite-worthy
# body handed back so the tool result stays compact (the tool trims further for
# the model reply; the full passage travels to the web citation).
_PASSAGE_BUDGET = 1500


class WebSearchService:
    """Orchestrates one governed web search (ADR-0014 §3/§4).

    Constructed per run with the tenant to rate-limit, the provider ``client``, the
    ``rate_limiter`` seam (a test fakes it — no Redis), and the fetch knobs
    (``fetch_top_n`` result pages to retrieve through the SSRF chokepoint, the
    outbound ``user_agent``). ``search`` is the single entry point.
    """

    def __init__(
        self,
        *,
        tenant_id: UUID,
        client: WebSearchClient,
        rate_limiter: RateLimiter,
        default_k: int,
        max_k: int,
        fetch_top_n: int,
        user_agent: str,
    ) -> None:
        self._tenant_id = tenant_id
        self._client = client
        self._rate_limiter = rate_limiter
        self._default_k = default_k
        self._max_k = max_k
        self._fetch_top_n = fetch_top_n
        self._user_agent = user_agent

    def _clamp_k(self, requested: int | None) -> int:
        if requested is None:
            return self._default_k
        return max(1, min(self._max_k, requested))

    async def search(
        self, query: str, *, k: int | None = None
    ) -> tuple[WebSearchResult, ...]:
        """Run one rate-limited web search, optionally enriching top results.

        Order: (1) admission-check the per-tenant window (ADR-0014 §3) — an
        exhausted window raises :class:`WebSearchRateLimited`; (2) query the
        provider (top-``k``); (3) fetch + extract the top ``fetch_top_n`` result
        pages **concurrently** through the SSRF chokepoint, attaching the
        extracted passage to each (a blocked/failed page is skipped, snippet
        retained). The returned tuple keeps the provider's ranking regardless of
        the order the pages happen to come back in.

        Raises:
            WebSearchRateLimited: the tenant's window is exhausted (throttled).
            WebSearchUnavailable: the provider was unreachable/errored (from the
                client) — the tool maps it to an ``ok=False`` result.
        """
        # (1) Per-tenant admission (ADR-0014 §3). Counts the search *before* any
        # outbound request so a throttled tenant makes neither the query nor any
        # result-page fetch. Fail-open on a Redis outage lives inside the limiter.
        if not self._rate_limiter.try_acquire(self._tenant_id):
            raise WebSearchRateLimited("web search rate limit exceeded for this tenant")

        # (2) Query leg — trusted internal hop to our own SearXNG.
        results = await self._client.search(query, k=self._clamp_k(k))
        if not results or self._fetch_top_n <= 0:
            return results

        # (3) Result-page fetch leg — the untrusted leg, ALWAYS through the one
        # SSRF chokepoint. Enrich the top N together; a blocked/failed page is
        # skipped. ``_fetch_passage`` absorbs each page's own failure and returns
        # None, so no one hostile or dead result can abort the gather and take
        # the whole search down with it (ADR-0014 §4).
        fetched, rest = results[: self._fetch_top_n], results[self._fetch_top_n :]
        async with httpx.AsyncClient(follow_redirects=False) as fetch_client:
            tasks = [
                asyncio.create_task(self._fetch_passage(r.url, client=fetch_client))
                for r in fetched
            ]
            try:
                passages = await asyncio.gather(*tasks)
            except BaseException:
                # A failure ``_fetch_passage`` does not absorb (or cancellation of
                # this search) would otherwise leave siblings running while the
                # ``async with`` closes the client out from under them. Retire
                # them here so the shared client only closes once nothing is
                # using it — the serial loop never left a fetch in flight either.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        # Zip strictly back onto the rows they came from: gather preserves the
        # argument order, so ranking survives whatever order the pages returned in.
        enriched = [
            result if passage is None else replace(result, fetched_passage=passage)
            for result, passage in zip(fetched, passages, strict=True)
        ]
        return (*enriched, *rest)

    async def _fetch_passage(
        self, url: str, *, client: httpx.AsyncClient
    ) -> str | None:
        """Fetch + extract one result page through the SSRF chokepoint, or ``None``.

        Returns the extracted, budget-trimmed passage text on success. A page that
        the SSRF guard blocks (``UrlBlockedError``), that is unreachable / non-2xx
        (``ConnectorError``), or that extracts to nothing is skipped (``None``) so
        one hostile or dead result never fails the whole search (ADR-0014 §4).
        """
        try:
            fetched = await fetch_url(url, client=client, user_agent=self._user_agent)
        except (UrlBlockedError, ConnectorError):
            return None
        extracted = extract_page_text(fetched.text, content_type=fetched.content_type)
        body = extracted.text.strip()
        if not body:
            return None
        if len(body) > _PASSAGE_BUDGET:
            body = body[:_PASSAGE_BUDGET].rstrip() + "…"
        return body


def build_web_search_service(
    settings: Settings, *, tenant_id: UUID, client: WebSearchClient | None = None
) -> WebSearchService:
    """Assemble a :class:`WebSearchService` from settings (the production wiring).

    Builds the SearXNG client (unless one is injected — a test passes a fake) and
    the Redis fixed-window rate limiter from ``settings``. Config is the single env
    reader (``core/config.py``); callers pass a ``Settings`` in rather than reading
    the environment themselves (backend/AGENTS.md).
    """
    provider = client or SearxngClient(
        settings.web_search_endpoint,
        timeout_seconds=settings.web_search_timeout_seconds,
        user_agent=settings.web_user_agent,
    )
    rate_limiter = RedisFixedWindowRateLimiter(
        settings.redis_url,
        max_per_window=settings.web_search_rate_max_per_window,
        window_seconds=settings.web_search_rate_window_seconds,
    )
    return WebSearchService(
        tenant_id=tenant_id,
        client=provider,
        rate_limiter=rate_limiter,
        default_k=settings.web_search_default_k,
        max_k=settings.web_search_max_k,
        fetch_top_n=settings.web_search_fetch_top_n,
        user_agent=settings.web_user_agent,
    )


__all__ = ["WebSearchService", "build_web_search_service"]
