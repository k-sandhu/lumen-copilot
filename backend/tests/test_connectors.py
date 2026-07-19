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

from app.connectors.base import (
    Connector,
    ConnectorConfigError,
    ConnectorError,
    ConnectorRun,
    FetchedDoc,
)
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

# The framework-supplied per-run context (ADR-0019 §4). The web connector uses
# its own SSRF-guarded fetch client, so this anonymous context is never dialled —
# a MockTransport that would fail loudly if it ever were.
_RUN = ConnectorRun(
    http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
)


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


def test_validate_config_normalizes_url_and_seeds_mode() -> None:
    """The stored config is the trimmed ``{url, mode}`` — mode is contract-required.

    Regression for the review finding that ``mode`` was omitted from the
    pending-source response (the frozen SourceConfig marks it required). The
    plain page URL seeds ``mode=page`` (refined later during sync).
    """
    out = WebConnector().validate_config({"url": "  https://example.com/a  "})
    assert out == {"url": "https://example.com/a", "mode": "page"}


@pytest.mark.parametrize(
    "url,expected_mode",
    [
        ("https://example.com/blog/post", "page"),  # ordinary page
        ("https://example.com/sitemap.xml", "sitemap"),  # .xml file
        ("https://example.com/news/sitemap_index.xml", "sitemap"),  # sitemap in path + .xml
        ("https://example.com/feeds/all.atom.xml", "sitemap"),  # any .xml → sitemap first
        ("https://example.com/blog/rss", "feed"),  # rss in path
        ("https://example.com/atom", "feed"),  # atom in path
        ("https://example.com/feed/", "feed"),  # feed in path
        ("https://example.com/posts?format=rss", "feed"),  # rss in query
    ],
)
def test_validate_config_seeds_mode_from_url_heuristic(url: str, expected_mode: str) -> None:
    """``mode`` is populated at creation from a cheap URL heuristic (ADR-0009 §2).

    Every stored web-source config carries a non-null ``mode`` *before* any fetch,
    so every ``/sources`` response is contract-valid. The first sync refines it.
    """
    out = WebConnector().validate_config({"url": url})
    assert out["mode"] == expected_mode


@pytest.mark.parametrize("bad", [{}, {"url": ""}, {"url": "   "}, {"url": 123}])
def test_validate_config_rejects_missing_url(bad: dict[str, object]) -> None:
    with pytest.raises(ConnectorConfigError):
        WebConnector().validate_config(bad)


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "ftp://example.com/x", "http://169.254.169.254/x"],
)
def test_validate_config_rejects_ssrf_url(url: str) -> None:
    """The request-path pre-check rejects IP-literal SSRF + bad schemes (no DNS)."""
    with pytest.raises(ConnectorConfigError) as exc:
        WebConnector().validate_config({"url": url})
    assert exc.value.code == "url_blocked"


def test_validate_config_does_no_dns_on_request_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: ``validate_config`` must not call ``getaddrinfo`` (no blocking DNS).

    The review flagged synchronous DNS on the POST /sources path (a blocking
    ``socket.getaddrinfo`` with no timeout). The request-path pre-check is now
    bounded + non-blocking — DNS is deferred to the Celery fetch path. We assert
    no resolution happens here by exploding if ``getaddrinfo`` is ever called.
    """
    import socket

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("validate_config must not resolve DNS on the request path")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    # A hostname (not an IP literal) would historically have triggered DNS here.
    out = WebConnector().validate_config({"url": "https://example.com/some/page"})
    assert out["url"] == "https://example.com/some/page"
    assert out["mode"] == "page"


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
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/page"), _RUN))
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
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/feed.xml"), _RUN))
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
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/feed.xml"), _RUN))
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
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/sitemap.xml"), _RUN))
    assert len(docs) == 2


async def test_health_reports_unhealthy_on_block(patched_client: dict[str, object]) -> None:
    patched_client["handler"] = lambda r: httpx.Response(200, headers={"content-type": "text/html"})
    # A blocked URL never reaches the transport — validate-time block at fetch.
    health = await WebConnector().health(_source("http://127.0.0.1/x"), _RUN)
    assert health.healthy is False


# --- Non-2xx status gate + outbound User-Agent (regression: issue #138) ------


async def test_sync_root_http_error_fails_the_sync(patched_client: dict[str, object]) -> None:
    """A root page returning a non-2xx fails the whole sync — the HTTP error page
    is never ingested as the document (#138)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"content-type": "text/html"},
            text="<html><body><p>Please set a user-agent.</p></body></html>",
        )

    patched_client["handler"] = handler
    with pytest.raises(ConnectorError) as exc:
        await WebConnector().sync(_source(f"http://{_PUBLIC}/page"), _RUN)
    assert exc.value.code == "fetch_failed"


async def test_sync_feed_skips_child_http_error(patched_client: dict[str, object]) -> None:
    """A feed item returning a non-2xx is skipped (best-effort child), not fatal (#138)."""
    feed = (
        "<rss version='2.0'><channel>"
        f"<item><title>Good</title><link>http://{_PUBLIC}/good</link></item>"
        f"<item><title>Bad</title><link>http://{_PUBLIC}/bad</link></item>"
        "</channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/good":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>ok</p>")
        if request.url.path == "/bad":
            return httpx.Response(500, headers={"content-type": "text/html"}, text="<p>boom</p>")
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=feed)

    patched_client["handler"] = handler
    docs = list(await WebConnector().sync(_source(f"http://{_PUBLIC}/feed.xml"), _RUN))
    assert [d.title for d in docs] == ["Good"]


async def test_sync_sends_configured_user_agent(patched_client: dict[str, object]) -> None:
    """The connector sends the configured descriptive UA on outbound fetches (#138)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>ok</p>")

    patched_client["handler"] = handler
    await WebConnector().sync(_source(f"http://{_PUBLIC}/page"), _RUN)
    assert "LumenCopilot" in seen["ua"]
    assert "python-httpx" not in seen["ua"].lower()


def test_web_user_agent_default_is_version_stamped() -> None:
    """The default outbound UA is descriptive + version-stamped (config, #138)."""
    from app.core.config import Settings

    s = Settings()  # required fields come from the conftest-seeded test env
    assert s.web_user_agent == (
        f"LumenCopilot/{s.version} (+https://github.com/k-sandhu/lumen-copilot)"
    )


def test_web_user_agent_is_overridable() -> None:
    """The full UA string is overridable via WEB_USER_AGENT (config, #138)."""
    from app.core.config import Settings

    s = Settings(WEB_USER_AGENT="Custom/1.0 (+mailto:ops@example.test)")  # type: ignore[call-arg]
    assert s.web_user_agent == "Custom/1.0 (+mailto:ops@example.test)"
