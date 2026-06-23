"""The Web URL connector — the first connector (ADR-0009 §2).

Given a public URL, fetch it through the SSRF-guarded chokepoint
(:mod:`app.connectors.web.fetch`) and yield :class:`~app.connectors.base.FetchedDoc`
passages of readable text:

* a **single page** → one document;
* an **RSS/Atom feed** → one document per item (bounded);
* a **sitemap.xml** (or sitemap index) → one document per listed URL (bounded).

The mode is **detected from the fetched content** (ADR-0009 §2): the first fetch
is parsed as a feed, then a sitemap, else treated as a page. Child URLs (feed
items / sitemap entries) are each re-fetched through the **same** SSRF guard, so
a hostile feed pointing items at ``127.0.0.1`` or the metadata IP cannot pivot
the server. Domain types only (ADR-0004): nothing here leaks ``httpx`` /
``ElementTree`` upward.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx

from app.connectors.base import (
    Connector,
    ConnectorConfigError,
    ConnectorError,
    ConnectorHealth,
    FetchedDoc,
)
from app.connectors.web.extract import (
    FeedItem,
    extract_page_text,
    parse_feed,
    parse_sitemap,
)
from app.connectors.web.fetch import FetchResult, UrlBlockedError, fetch_url
from app.domain.entities import Source, WebSourceMode


class WebConnector:
    """The ``web`` connector (zero source-side setup; ADR-0009 §2).

    ``validate_config`` runs the SSRF pre-check in the request path so an invalid
    or blocked URL is a 422 *before* a row is written; ``sync`` re-fetches and
    expands feeds/sitemaps inside the Celery task.
    """

    name = "web"

    def validate_config(self, config: dict[str, object]) -> dict[str, object]:
        """Validate the ``{url}`` config; return the normalised config to store.

        The URL's scheme + host are validated synchronously (no network) via the
        SSRF guard's URL check; a blocked URL raises :class:`UrlBlockedError`
        (→ 422 ``url_blocked``). ``mode`` is left unset here — it is detected and
        recorded by the first ``sync`` once the content is fetched. A missing or
        non-string ``url`` is an invalid config (422).
        """
        url = config.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ConnectorConfigError(
                "a web source requires a non-empty 'url'", code="invalid_url"
            )
        normalized = url.strip()
        # Synchronous scheme/host SSRF pre-check (no network): rejects a non-http(s)
        # scheme or an IP-literal in a blocked range before a row is written. DNS
        # resolution + the full per-hop guard run again at fetch time (sync).
        from app.connectors.web.fetch import _validate_url

        _validate_url(normalized)
        return {"url": normalized}

    async def sync(self, source: Source) -> Iterable[FetchedDoc]:
        """Fetch the source URL, detect the mode, and yield its documents.

        Detection order (ADR-0009 §2): feed → sitemap → single page. For a feed or
        sitemap, each child URL is re-fetched through the same SSRF guard and
        becomes its own :class:`FetchedDoc`; a child that fails the guard or the
        fetch is skipped (best-effort) rather than failing the whole sync. A
        single page yields exactly one doc.

        Raises:
            ConnectorConfigError: the **root** URL is SSRF-blocked (url_blocked).
            ConnectorError: the root fetch failed at the transport level.
        """
        url = self._url_of(source)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            root = await fetch_url(url, client=client)
            docs = await self._expand(root, client)
        return docs

    async def health(self, source: Source) -> ConnectorHealth:
        """Probe the source URL: a successful guarded fetch is healthy.

        A cheap reachability/validity check for the connector grid (ADR-0009 §4).
        An SSRF rejection or a fetch fault is reported as unhealthy with the
        reason rather than raised.
        """
        url = self._url_of(source)
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                await fetch_url(url, client=client)
        except (ConnectorError, UrlBlockedError) as exc:
            return ConnectorHealth(healthy=False, detail=exc.detail)
        return ConnectorHealth(healthy=True)

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _url_of(source: Source) -> str:
        url = source.config.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ConnectorConfigError("source config has no usable 'url'", code="invalid_url")
        return url.strip()

    async def _expand(self, root: FetchResult, client: httpx.AsyncClient) -> list[FetchedDoc]:
        """Expand a fetched root into one or more :class:`FetchedDoc` by mode."""
        feed = parse_feed(root.text)
        if feed is not None and feed:
            return await self._docs_from_feed(feed, client)

        sitemap = parse_sitemap(root.text)
        if sitemap is not None and sitemap:
            return await self._docs_from_urls(sitemap, client)

        # Single page.
        page = extract_page_text(root.text, content_type=root.content_type)
        title = page.title or root.final_url
        return [FetchedDoc(title=title, text=page.text, url=root.final_url)]

    async def _docs_from_feed(
        self, items: list[FeedItem], client: httpx.AsyncClient
    ) -> list[FetchedDoc]:
        """Fetch each feed item's page (guarded); skip ones that fail."""
        docs: list[FetchedDoc] = []
        for item in items:
            try:
                fetched = await fetch_url(item.url, client=client)
            except (ConnectorError, UrlBlockedError):
                # A child that is SSRF-blocked or unreachable is skipped, not fatal.
                continue
            page = extract_page_text(fetched.text, content_type=fetched.content_type)
            title = item.title or page.title or fetched.final_url
            text = page.text or item.summary
            docs.append(FetchedDoc(title=title, text=text, url=fetched.final_url))
        return docs

    async def _docs_from_urls(self, urls: list[str], client: httpx.AsyncClient) -> list[FetchedDoc]:
        """Fetch each sitemap URL's page (guarded); skip ones that fail."""
        docs: list[FetchedDoc] = []
        for child in urls:
            try:
                fetched = await fetch_url(child, client=client)
            except (ConnectorError, UrlBlockedError):
                continue
            page = extract_page_text(fetched.text, content_type=fetched.content_type)
            title = page.title or fetched.final_url
            docs.append(FetchedDoc(title=title, text=page.text, url=fetched.final_url))
        return docs


# Module-level connector instance the registry auto-discovers (ADR-0008 §3).
CONNECTOR: Connector = WebConnector()


def detect_mode(text: str, content_type: str) -> WebSourceMode:
    """Classify fetched content as ``feed`` / ``sitemap`` / ``page`` (ADR-0009 §2).

    The mode recorded on the source after the first sync, surfaced in the grid.
    Mirrors the detection order ``_expand`` uses.
    """
    if parse_feed(text):
        return WebSourceMode.FEED
    if parse_sitemap(text):
        return WebSourceMode.SITEMAP
    return WebSourceMode.PAGE


__all__ = ["CONNECTOR", "WebConnector", "detect_mode"]
