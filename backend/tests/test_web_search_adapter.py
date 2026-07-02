"""The web-search adapter — mapping, rate limit, and SSRF egress (ADR-0014, #219).

All offline: the SearXNG call is driven by an ``httpx.MockTransport`` and the
result-page fetch is driven by another, so no real socket opens. Asserts:

* the client maps SearXNG JSON into the **domain** ``WebSearchResult`` and **no
  vendor/response type leaks** upward (the returned tuple is domain types only);
* the per-tenant rate limiter is enforced — an exhausted window raises
  ``WebSearchRateLimited`` and NO outbound query is made (ADR-0014 §3, AC-4);
* the result-page fetch leg goes through the ``connectors/web/fetch.py`` SSRF
  chokepoint — a result whose URL redirects to a loopback/metadata/private target
  is **rejected by the guard and skipped** (its ``fetched_passage`` stays ``None``,
  the snippet stands), so a hostile result never opens a second egress path
  (ADR-0014 §3, AC-2).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import httpx
import pytest

from app.domain.web_search import WebSearchResult
from app.search_web.client import (
    SearxngClient,
    WebSearchUnavailable,
    map_searxng_results,
)
from app.search_web.service import WebSearchService

_PUBLIC_IP = "93.184.216.34"


class _AlwaysAllow:
    """A rate limiter that admits every search (the happy-path fake)."""

    def try_acquire(self, tenant_id: uuid.UUID) -> bool:  # noqa: ARG002
        return True


class _AlwaysDeny:
    """A rate limiter whose window is exhausted — every search is throttled."""

    def try_acquire(self, tenant_id: uuid.UUID) -> bool:  # noqa: ARG002
        return False


class _FakeClient:
    """A web-search client returning fixed domain results (no network)."""

    def __init__(self, results: tuple[WebSearchResult, ...]) -> None:
        self._results = results
        self.calls = 0

    async def search(self, query: str, *, k: int) -> tuple[WebSearchResult, ...]:  # noqa: ARG002
        self.calls += 1
        return self._results[:k]


# --- Client mapping: SearXNG JSON -> domain (no vendor leak) -----------------


_SEARX_JSON = {
    "query": "python",
    "results": [
        {
            "title": "Python.org",
            "url": "https://www.python.org/",
            "content": "The official home of the Python programming language.",
            "publishedDate": "2026-01-02T10:00:00Z",
        },
        {
            "title": "Wikipedia",
            "url": "https://en.wikipedia.org/wiki/Python",
            "content": "Python is a high-level language.",
        },
        # A non-http(s) result is dropped (never fetchable/citable).
        {"title": "bad", "url": "ftp://example.com/x", "content": "nope"},
        # A result missing a url is skipped.
        {"title": "no url", "content": "orphan"},
    ],
}


def test_map_returns_domain_type_only() -> None:
    """The mapped results are the domain ``WebSearchResult`` — no vendor type leaks."""
    results = map_searxng_results(_SEARX_JSON, k=10)
    assert all(isinstance(r, WebSearchResult) for r in results)
    # ftp + url-less entries dropped; two http(s) results survive.
    assert [r.url for r in results] == [
        "https://www.python.org/",
        "https://en.wikipedia.org/wiki/Python",
    ]
    assert results[0].title == "Python.org"
    assert results[0].snippet.startswith("The official home")
    assert isinstance(results[0].published_at, datetime)
    # No page was fetched by the pure mapper — passage stays None (INV-3).
    assert results[0].fetched_passage is None
    # A missing publishedDate is None, never an error.
    assert results[1].published_at is None


def test_map_truncates_to_k() -> None:
    assert len(map_searxng_results(_SEARX_JSON, k=1)) == 1


def test_map_non_object_body_is_unavailable() -> None:
    with pytest.raises(WebSearchUnavailable):
        map_searxng_results(["not", "an", "object"], k=5)


def test_map_missing_results_is_empty_not_error() -> None:
    assert map_searxng_results({"query": "x"}, k=5) == ()


async def _searx_client(handler: object, **kw: object) -> SearxngClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=transport)
    return SearxngClient(
        "http://searxng:8080", timeout_seconds=5.0, user_agent="LumenTest/1", client=client, **kw  # type: ignore[arg-type]
    )


async def test_client_queries_searxng_json_and_maps() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["format"] = request.url.params.get("format", "")
        return httpx.Response(200, json=_SEARX_JSON)

    client = await _searx_client(handler)
    results = await client.search("python", k=5)
    assert seen["path"] == "/search"
    assert seen["format"] == "json"  # JSON output is the contract (ADR-0014 §1)
    assert all(isinstance(r, WebSearchResult) for r in results)


async def test_client_non_2xx_is_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    client = await _searx_client(handler)
    with pytest.raises(WebSearchUnavailable):
        await client.search("python", k=5)


async def test_client_transport_error_is_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = await _searx_client(handler)
    with pytest.raises(WebSearchUnavailable):
        await client.search("python", k=5)


# --- Rate limit (ADR-0014 §3 / AC-4) ----------------------------------------


async def test_rate_limited_search_raises_and_makes_no_call() -> None:
    """An exhausted per-tenant window throttles the search before any egress."""
    from app.search_web.client import WebSearchRateLimited

    client = _FakeClient((WebSearchResult(title="t", url=f"https://{_PUBLIC_IP}/", snippet="s"),))
    service = WebSearchService(
        tenant_id=uuid.uuid4(),
        client=client,
        rate_limiter=_AlwaysDeny(),
        default_k=5,
        max_k=10,
        fetch_top_n=0,
        user_agent="LumenTest/1",
    )
    with pytest.raises(WebSearchRateLimited):
        await service.search("python")
    # The throttled tenant made neither a query nor a result-page fetch.
    assert client.calls == 0


async def test_admitted_search_returns_results_snippets_only() -> None:
    client = _FakeClient(
        (
            WebSearchResult(title="a", url="https://a.example/", snippet="alpha"),
            WebSearchResult(title="b", url="https://b.example/", snippet="beta"),
        )
    )
    service = WebSearchService(
        tenant_id=uuid.uuid4(),
        client=client,
        rate_limiter=_AlwaysAllow(),
        default_k=5,
        max_k=10,
        fetch_top_n=0,  # snippets only, no page fetch
        user_agent="LumenTest/1",
    )
    results = await service.search("python")
    assert client.calls == 1
    assert [r.url for r in results] == ["https://a.example/", "https://b.example/"]
    assert all(r.fetched_passage is None for r in results)


def test_service_clamps_k_to_max() -> None:
    service = WebSearchService(
        tenant_id=uuid.uuid4(),
        client=_FakeClient(()),
        rate_limiter=_AlwaysAllow(),
        default_k=5,
        max_k=10,
        fetch_top_n=0,
        user_agent="LumenTest/1",
    )
    assert service._clamp_k(None) == 5  # default when unset
    assert service._clamp_k(100) == 10  # clamped to max
    assert service._clamp_k(0) == 1  # floored to 1
    assert service._clamp_k(3) == 3


# --- SSRF egress: the result-page fetch leg goes through the chokepoint ------
# (AC-2 — the mandatory negative; reuses connectors/web/fetch.py verbatim.)


class _MonkeyFetchService(WebSearchService):
    """Drives the real fetch chokepoint against a MockTransport for the fetch leg."""


@pytest.mark.parametrize(
    "redirect_target",
    [
        "http://127.0.0.1/secret",  # loopback
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.9/internal",  # RFC-1918 private
    ],
)
async def test_result_page_redirect_to_blocked_target_is_skipped(
    monkeypatch: pytest.MonkeyPatch, redirect_target: str
) -> None:
    """A result page that redirects to a blocked range is refused by the SSRF guard.

    The service reuses ``connectors/web/fetch.py``; the guard re-validates every
    redirect hop, so a public result URL that 30x-redirects to loopback/metadata/
    private is rejected. The service catches that and **skips** the page (its
    ``fetched_passage`` stays None, the snippet stands) — one hostile result never
    fails the whole search, and NO fetch to the blocked target is followed.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        # Every fetched result page tries to redirect to the blocked target.
        return httpx.Response(302, headers={"location": redirect_target})

    # Pin the service's internal fetch httpx client to the MockTransport so the
    # real fetch_url guard runs offline against our handler.
    real_async_client = httpx.AsyncClient

    def _mock_client(*_a: object, **kw: object) -> httpx.AsyncClient:
        kw.pop("follow_redirects", None)
        return real_async_client(transport=httpx.MockTransport(handler), follow_redirects=False)

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client)

    client = _FakeClient(
        (WebSearchResult(title="t", url=f"http://{_PUBLIC_IP}/page", snippet="snip"),)
    )
    service = WebSearchService(
        tenant_id=uuid.uuid4(),
        client=client,
        rate_limiter=_AlwaysAllow(),
        default_k=5,
        max_k=10,
        fetch_top_n=1,  # fetch the top page — through the SSRF chokepoint
        user_agent="LumenTest/1",
    )
    results = await service.search("python")
    assert len(results) == 1
    # The blocked redirect was refused by the guard; the page was skipped, the
    # snippet retained, and no fetched passage attached (INV-3 + SSRF).
    assert results[0].fetched_passage is None
    assert results[0].snippet == "snip"


async def test_result_page_fetch_success_attaches_passage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public, allowed result page is fetched+extracted and its passage attached."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Doc</title></head>"
                "<body><p>Extracted body text.</p></body></html>"
            ),
        )

    real_async_client = httpx.AsyncClient

    def _mock_client(*_a: object, **kw: object) -> httpx.AsyncClient:
        kw.pop("follow_redirects", None)
        return real_async_client(transport=httpx.MockTransport(handler), follow_redirects=False)

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client)

    client = _FakeClient(
        (WebSearchResult(title="t", url=f"http://{_PUBLIC_IP}/page", snippet="snip"),)
    )
    service = WebSearchService(
        tenant_id=uuid.uuid4(),
        client=client,
        rate_limiter=_AlwaysAllow(),
        default_k=5,
        max_k=10,
        fetch_top_n=1,
        user_agent="LumenTest/1",
    )
    results = await service.search("python")
    assert results[0].fetched_passage is not None
    assert "Extracted body text." in results[0].fetched_passage
