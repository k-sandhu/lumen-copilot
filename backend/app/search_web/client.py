"""The web-search provider client — SearXNG behind a swappable seam (ADR-0014 §1/§2).

Owns the HTTP call to the search provider and maps its **JSON** response into the
domain :class:`~app.domain.web_search.WebSearchResult`. Nobody else may talk to a
web-search provider (AGENTS.md §6 new boundary row). Swapping SearXNG for a hosted
API (Tavily/Brave/Bing) is a new :class:`WebSearchClient` implementation behind the
same :class:`~typing.Protocol` — the service, the tool, and the runtime do not
change, because the only type that crosses the boundary is ``WebSearchResult``.

Errors are typed and vendor-free: a provider that is unreachable / returns a
non-2xx / returns unparseable JSON raises :class:`WebSearchUnavailable` (the tool
turns it into an ``ok=False`` result, never a crash). No SearXNG/``httpx`` object
is ever raised or returned.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.domain.web_search import WebSearchResult


class WebSearchError(Exception):
    """Base class for a web-search failure (domain error — no vendor type leaks)."""


class WebSearchUnavailable(WebSearchError):
    """The search provider was unreachable, errored, or returned unusable data.

    Raised for a connection/timeout fault, a non-2xx response, or a JSON body the
    adapter cannot parse. The ``web_search`` tool maps this to an ``ok=False``
    result (the model reads it; the run continues) rather than crashing the stream.
    """


class WebSearchRateLimited(WebSearchError):
    """The per-tenant web-search window is exhausted (ADR-0014 §3, throttled).

    The service raises this when the Redis fixed-window limiter refuses the search;
    the tool surfaces it as a distinct ``ok=False`` result so the model learns the
    call was throttled (not that the web is empty).
    """


class WebSearchClient(Protocol):
    """The provider seam every web-search client satisfies (ADR-0014 §1).

    ``search`` issues one query and returns an ordered tuple of domain
    :class:`WebSearchResult` (top-``k`` by provider rank). An implementation raises
    :class:`WebSearchUnavailable` on any provider fault — never a vendor exception.
    """

    async def search(self, query: str, *, k: int) -> tuple[WebSearchResult, ...]: ...


def _only_https_or_http(url: str) -> str | None:
    """Return ``url`` iff it is an ``http``/``https`` URL with a host, else ``None``.

    A result URL that is not fetchable/citable (a non-web scheme, or hostless) is
    dropped from the mapped results so nothing downstream tries to fetch it — the
    ``connectors/web/fetch.py`` chokepoint would reject it anyway, but filtering
    here keeps the returned set clean and every ``url`` a real web address.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if not parts.hostname:
        return None
    return url


def _parse_published(value: object) -> datetime | None:
    """Parse SearXNG's ``publishedDate`` (ISO-8601) into a ``datetime`` or ``None``.

    SearXNG reports the field only for some engines; a missing/unparseable value
    yields ``None`` (the domain type's default) rather than raising — a bad date
    must never fail an otherwise-good search.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # ``fromisoformat`` accepts a trailing ``Z`` only from 3.11+; normalise it.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def map_searxng_results(payload: Any, *, k: int) -> tuple[WebSearchResult, ...]:  # noqa: ANN401
    """Map a SearXNG JSON body into the ordered domain results (top-``k``).

    Reads the provider's ``results`` array (``title`` / ``url`` / ``content`` →
    ``snippet`` / ``publishedDate``), drops any entry without an ``http(s)`` URL,
    and truncates to ``k`` in provider-rank order. Anything the adapter cannot make
    sense of is treated as "no results" (an empty tuple), never an exception —
    the caller distinguishes empty from unavailable.
    """
    if not isinstance(payload, dict):
        raise WebSearchUnavailable("search provider returned a non-object JSON body")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return ()
    mapped: list[WebSearchResult] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        safe_url = _only_https_or_http(url)
        if safe_url is None:
            continue
        title = entry.get("title")
        snippet = entry.get("content")
        mapped.append(
            WebSearchResult(
                title=str(title).strip() if isinstance(title, str) else "",
                url=safe_url,
                snippet=str(snippet).strip() if isinstance(snippet, str) else "",
                published_at=_parse_published(entry.get("publishedDate")),
            )
        )
        if len(mapped) >= k:
            break
    return tuple(mapped)


class SearxngClient:
    """The SearXNG JSON client (the OSS provider, ADR-0014 §1).

    Queries a SearXNG instance's ``/search`` endpoint with ``format=json`` and maps
    the structured results into :class:`WebSearchResult`. No API key (that is the
    point of SearXNG). The ``httpx.AsyncClient`` is injectable so tests drive it
    with a ``MockTransport`` (fully offline); production builds one bounded by the
    configured timeout. The endpoint is an internal service address, so this is the
    trusted **query leg** (ADR-0014 §3) — distinct from the untrusted result-page
    fetch, which the service routes through the SSRF chokepoint.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent
        self._client = client

    async def search(self, query: str, *, k: int) -> tuple[WebSearchResult, ...]:
        """Issue one SearXNG query; return the mapped top-``k`` domain results.

        Raises:
            WebSearchUnavailable: the instance is unreachable, returned a non-2xx,
                or returned a body that is not usable JSON.
        """
        params = {"q": query, "format": "json"}
        owns_client = self._client is None
        active = self._client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            try:
                response = await active.get(
                    f"{self._endpoint}/search",
                    params=params,
                    headers={"User-Agent": self._user_agent, "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise WebSearchUnavailable(
                    f"search provider request failed: {type(exc).__name__}"
                ) from exc
            if response.status_code // 100 != 2:
                raise WebSearchUnavailable(
                    f"search provider returned HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise WebSearchUnavailable("search provider returned invalid JSON") from exc
        finally:
            if owns_client:
                await active.aclose()
        return map_searxng_results(payload, k=k)


__all__ = [
    "SearxngClient",
    "WebSearchClient",
    "WebSearchError",
    "WebSearchRateLimited",
    "WebSearchUnavailable",
    "map_searxng_results",
]
