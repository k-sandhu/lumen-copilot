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

import asyncio
import uuid
from datetime import datetime

import httpx
import pytest

from app.connectors.base import ConnectorError
from app.connectors.web.fetch import FetchResult, UrlBlockedError
from app.domain.web_search import WebSearchResult
from app.search_web.client import (
    SearxngClient,
    WebSearchRateLimited,
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


# --- Fetch-leg concurrency (#513) -------------------------------------------
#
# The top-N result pages are independent egress calls to different untrusted
# third-party hosts: no shared state, no ordering dependency. Fetched one after
# another the leg costs the SUM of the pages, and since these are arbitrary
# internet hosts, slow and dead ones are the norm — with the default 10 s
# per-fetch budget, three dead hosts serialize into ~30 s inside a live chat
# turn. Fetched together it costs about the slowest single page.
#
# The probe replaces the SSRF chokepoint (`fetch_url`) with a fake that records
# how many fetches are in flight at once. Every fake fetch parks on the same
# asyncio.Event, so a serial loop can never exceed one in flight, while a
# concurrent one necessarily reaches all three before any completes. That makes
# the assertion structural rather than a timing race.


class _FetchProbe:
    """Records peak in-flight fetches; releases only once ``expected`` arrive."""

    def __init__(self, expected: int) -> None:
        self._expected = expected
        self._all_arrived = asyncio.Event()
        self.in_flight = 0
        self.max_in_flight = 0
        self.urls: list[str] = []

    async def fetch(self, url: str, **_kw: object) -> FetchResult:
        self.urls.append(url)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight >= self._expected:
            self._all_arrived.set()
        try:
            # A serial implementation never sets the event, so it times out here
            # and the peak stays at 1 — the failure this test exists to catch.
            await asyncio.wait_for(self._all_arrived.wait(), timeout=_PROBE_TIMEOUT)
        except TimeoutError:
            pass
        finally:
            self.in_flight -= 1
        return FetchResult(
            url=url,
            final_url=url,
            content_type="text/html",
            text=f"<html><body><p>body of {url}</p></body></html>",
        )


_PROBE_TIMEOUT = 2.0


def _probe_service(fetch_top_n: int, results: tuple[WebSearchResult, ...]) -> WebSearchService:
    return WebSearchService(
        tenant_id=uuid.uuid4(),
        client=_FakeClient(results),
        rate_limiter=_AlwaysAllow(),
        default_k=5,
        max_k=10,
        fetch_top_n=fetch_top_n,
        user_agent="LumenTest/1",
    )


_THREE_RESULTS = (
    WebSearchResult(title="a", url="https://a.example/", snippet="alpha"),
    WebSearchResult(title="b", url="https://b.example/", snippet="beta"),
    WebSearchResult(title="c", url="https://c.example/", snippet="gamma"),
)


async def test_result_page_fetches_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """All ``fetch_top_n`` pages are in flight together, not one after another."""
    probe = _FetchProbe(expected=3)
    monkeypatch.setattr("app.search_web.service.fetch_url", probe.fetch)

    results = await _probe_service(3, _THREE_RESULTS).search("python")

    assert probe.max_in_flight == 3
    assert len(results) == 3


async def test_concurrent_fetches_preserve_provider_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Results keep the provider's order however the fetches interleave.

    Concurrency must not become a reordering: the ranking is the provider's, and
    a citation's position is part of what the model is grounding on.
    """
    probe = _FetchProbe(expected=3)
    monkeypatch.setattr("app.search_web.service.fetch_url", probe.fetch)

    results = await _probe_service(3, _THREE_RESULTS).search("python")

    assert [r.url for r in results] == [
        "https://a.example/",
        "https://b.example/",
        "https://c.example/",
    ]
    # Each passage is the one extracted from that result's own page — the
    # gather's results are zipped back to the right rows, not shifted.
    for result in results:
        assert result.fetched_passage is not None
        assert f"body of {result.url}" in result.fetched_passage


async def test_only_the_top_n_are_fetched_when_more_results_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap still bounds egress; the tail keeps its snippet and no passage."""
    probe = _FetchProbe(expected=2)
    monkeypatch.setattr("app.search_web.service.fetch_url", probe.fetch)

    results = await _probe_service(2, _THREE_RESULTS).search("python")

    assert probe.max_in_flight == 2
    assert sorted(probe.urls) == ["https://a.example/", "https://b.example/"]
    assert results[2].fetched_passage is None
    assert results[2].snippet == "gamma"


@pytest.mark.parametrize("failure", [UrlBlockedError("blocked"), ConnectorError("unreachable")])
async def test_one_failing_page_is_skipped_without_failing_the_search(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Per-page failure isolation survives concurrency (ADR-0014 §4).

    Gathering raises the first exception by default; the middle page failing must
    still leave the other two enriched and the search successful, exactly as the
    serial loop's per-page try/except did.
    """
    probe = _FetchProbe(expected=3)
    real_fetch = probe.fetch

    async def _fetch(url: str, **kw: object) -> FetchResult:
        if url == "https://b.example/":
            # Keep the peak-concurrency accounting honest: this page still
            # occupies a slot, it just ends in a refusal.
            probe.in_flight += 1
            probe.max_in_flight = max(probe.max_in_flight, probe.in_flight)
            probe.in_flight -= 1
            raise failure
        return await real_fetch(url, **kw)

    monkeypatch.setattr("app.search_web.service.fetch_url", _fetch)

    # Only two pages ever park on the event, so let the probe release on two.
    probe._expected = 2  # noqa: SLF001

    results = await _probe_service(3, _THREE_RESULTS).search("python")

    assert [r.url for r in results] == [
        "https://a.example/",
        "https://b.example/",
        "https://c.example/",
    ]
    # The blocked/unreachable page is skipped: no passage, snippet retained.
    assert results[1].fetched_passage is None
    assert results[1].snippet == "beta"
    # Its neighbours are unaffected.
    assert results[0].fetched_passage is not None
    assert results[2].fetched_passage is not None


async def test_rate_limit_still_precedes_every_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A throttled tenant runs no fetches at all — admission stays ahead of egress."""
    probe = _FetchProbe(expected=3)
    monkeypatch.setattr("app.search_web.service.fetch_url", probe.fetch)

    service = WebSearchService(
        tenant_id=uuid.uuid4(),
        client=_FakeClient(_THREE_RESULTS),
        rate_limiter=_AlwaysDeny(),
        default_k=5,
        max_k=10,
        fetch_top_n=3,
        user_agent="LumenTest/1",
    )
    with pytest.raises(WebSearchRateLimited):
        await service.search("python")
    assert probe.urls == []
