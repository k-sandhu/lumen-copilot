"""Connector framework + web connector tests (#20, ADR-0009 §1/§2).

Covers the auto-discovered registry, the protocol surface, and the web
connector's three modes (page / feed / sitemap) plus its HTML→text and feed/
sitemap extraction. All offline: the connector's fetches are driven by an
``httpx.MockTransport`` (patched into the connector via a monkeypatched client),
so no real socket opens. Child-URL SSRF skipping is asserted too (a feed item
pointing at a blocked address is dropped, never fetched).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest

from app.connectors.base import Connector, ConnectorConfigError, FetchedDoc
from app.connectors.registry import (
    UnknownConnectorError,
    get_connector,
    registered_types,
)
from app.connectors.web.connector import WebConnector, detect_mode
from app.connectors.web.extract import (
    extract_page_text,
    parse_feed,
    parse_sitemap,
)
from app.domain.entities import Source, SourceStatus, WebSourceMode

_PUBLIC = "93.184.216.34"


def _source(url: str) -> Source:
    now = datetime.now(UTC)
    return Source(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        type="web",
        config={"url": url},
        status=SourceStatus.PENDING,
        indexed_count=0,
        last_synced_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )


# --- Registry ---------------------------------------------------------------


def test_registry_discovers_web() -> None:
    assert "web" in registered_types()


def test_get_connector_returns_web() -> None:
    connector = get_connector("web")
    assert connector.name == "web"
    assert isinstance(connector, Connector)  # runtime_checkable protocol


def test_get_unknown_connector_raises() -> None:
    with pytest.raises(UnknownConnectorError):
        get_connector("does-not-exist")


# --- validate_config (the request-path SSRF pre-check) ----------------------


def test_validate_config_normalizes_url() -> None:
    out = WebConnector().validate_config({"url": "  https://example.com/a  "})
    assert out == {"url": "https://example.com/a"}


@pytest.mark.parametrize("bad", [{}, {"url": ""}, {"url": "   "}, {"url": 123}])
def test_validate_config_rejects_missing_url(bad: dict[str, object]) -> None:
    with pytest.raises(ConnectorConfigError):
        WebConnector().validate_config(bad)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "ftp://example.com/x", "http://169.254.169.254/x"],
)
def test_validate_config_rejects_ssrf_url(url: str) -> None:
    with pytest.raises(ConnectorConfigError) as exc:
        WebConnector().validate_config({"url": url})
    assert exc.value.code == "url_blocked"


# --- HTML / feed / sitemap extraction (pure) --------------------------------


def test_extract_page_strips_scripts_and_keeps_title() -> None:
    html = (
        "<html><head><title>My Page</title><style>.x{}</style></head>"
        "<body><script>evil()</script><p>Hello</p><p>World</p></body></html>"
    )
    page = extract_page_text(html, content_type="text/html")
    assert page.title == "My Page"
    assert "Hello" in page.text and "World" in page.text
    assert "evil" not in page.text and ".x{}" not in page.text


def test_extract_plain_text_uses_first_line_as_title() -> None:
    page = extract_page_text("First line\n\nbody body", content_type="text/plain")
    assert page.title == "First line"
    assert "body body" in page.text


def test_parse_rss_feed_items() -> None:
    rss = """<?xml version='1.0'?>
    <rss version='2.0'><channel><title>Blog</title>
      <item><title>Post A</title><link>http://example.com/a</link>
        <description>Summary A</description></item>
      <item><title>Post B</title><link>http://example.com/b</link></item>
    </channel></rss>"""
    items = parse_feed(rss)
    assert items is not None
    assert [i.url for i in items] == ["http://example.com/a", "http://example.com/b"]
    assert items[0].title == "Post A"


def test_parse_atom_feed_uses_link_href() -> None:
    atom = """<?xml version='1.0'?>
    <feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><title>Entry 1</title>
        <link rel='alternate' href='http://example.com/1'/></entry>
    </feed>"""
    items = parse_feed(atom)
    assert items is not None
    assert items[0].url == "http://example.com/1"


def test_parse_sitemap_collects_locs() -> None:
    sitemap = """<?xml version='1.0'?>
    <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
      <url><loc>http://example.com/p1</loc></url>
      <url><loc>http://example.com/p2</loc></url>
    </urlset>"""
    locs = parse_sitemap(sitemap)
    assert locs == ["http://example.com/p1", "http://example.com/p2"]


def test_non_feed_non_sitemap_returns_none() -> None:
    assert parse_feed("<html></html>") is None
    assert parse_sitemap("<html></html>") is None
    assert parse_feed("not xml at all") is None


def test_detect_mode() -> None:
    assert detect_mode("<html><p>x</p></html>", "text/html") == WebSourceMode.PAGE
    rss = "<rss version='2.0'><channel><item><link>http://e.com/a</link></item></channel></rss>"
    assert detect_mode(rss, "application/rss+xml") == WebSourceMode.FEED
    sm = (
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        "<url><loc>http://e.com/p</loc></url></urlset>"
    )
    assert detect_mode(sm, "application/xml") == WebSourceMode.SITEMAP


# --- Web connector sync (MockTransport, offline) ----------------------------


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch ``httpx.AsyncClient`` so the connector's fetches hit a MockTransport.

    The connector constructs its own ``AsyncClient``; we replace the class with a
    factory that injects a mock transport whose handler the test sets. Returns a
    mutable dict so a test installs its ``handler``.
    """
    state: dict[str, object] = {"handler": None}

    real_client = httpx.AsyncClient

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        handler = state["handler"]
        kwargs.pop("follow_redirects", None)
        return real_client(transport=httpx.MockTransport(handler), follow_redirects=False)  # type: ignore[arg-type]

    monkeypatch.setattr("app.connectors.web.connector.httpx.AsyncClient", _factory)
    return state


async def test_sync_single_page(patched_client: dict[str, object]) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><head><title>Page</title></head><body><p>Content here</p></body></html>",
        )

    patched_client["handler"] = handler
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/page")))
    assert len(docs) == 1
    assert isinstance(docs[0], FetchedDoc)
    assert docs[0].title == "Page"
    assert "Content here" in docs[0].text


async def test_sync_feed_fans_out(patched_client: dict[str, object]) -> None:
    feed = (
        "<rss version='2.0'><channel>"
        f"<item><title>A</title><link>http://{_PUBLIC}/a</link></item>"
        f"<item><title>B</title><link>http://{_PUBLIC}/b</link></item>"
        "</channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/a", "/b"}:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text=f"<p>body {request.url.path}</p>"
            )
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=feed)

    patched_client["handler"] = handler
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/feed.xml")))
    assert {d.title for d in docs} == {"A", "B"}


async def test_sync_feed_skips_ssrf_child(patched_client: dict[str, object]) -> None:
    """A feed item pointing at a blocked address is dropped, not fetched."""
    feed = (
        "<rss version='2.0'><channel>"
        f"<item><title>Good</title><link>http://{_PUBLIC}/good</link></item>"
        "<item><title>Evil</title><link>http://169.254.169.254/latest/meta-data</link></item>"
        "</channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/good":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>ok</p>")
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=feed)

    patched_client["handler"] = handler
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/feed.xml")))
    assert [d.title for d in docs] == ["Good"]


async def test_sync_sitemap_fans_out(patched_client: dict[str, object]) -> None:
    sitemap = (
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
        f"<url><loc>http://{_PUBLIC}/x</loc></url>"
        f"<url><loc>http://{_PUBLIC}/y</loc></url>"
        "</urlset>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/x", "/y"}:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, text=f"<p>page {request.url.path}</p>"
            )
        return httpx.Response(200, headers={"content-type": "application/xml"}, text=sitemap)

    patched_client["handler"] = handler
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/sitemap.xml")))
    assert len(docs) == 2


async def test_health_reports_unhealthy_on_block(patched_client: dict[str, object]) -> None:
    patched_client["handler"] = lambda r: httpx.Response(200, headers={"content-type": "text/html"})
    # A blocked URL never reaches the transport — validate-time block at fetch.
    health = await WebConnector().health(_source("http://127.0.0.1/x"))
    assert health.healthy is False
