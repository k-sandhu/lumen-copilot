"""Content extraction for the web connector — HTML / RSS-Atom / sitemap (ADR-0009 §2).

Pure, stdlib-only parsing (no vendor SDK leak, ADR-0004; no new dependency): an
``html.parser`` HTML-to-text extractor and an ``xml.etree`` detector that tells a
**feed** (RSS/Atom → many docs) or a **sitemap** (sitemap.xml → many docs) from a
plain **page**. Nothing here does I/O — it operates on the text the SSRF-guarded
:mod:`app.connectors.web.fetch` already fetched.

The feed/sitemap parsers yield **bounded** URL lists (ADR-0009 §2 "bounded
count") so a hostile feed/sitemap with millions of entries cannot fan out into an
unbounded crawl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from xml.etree import ElementTree as ET  # noqa: N817 — conventional ElementTree alias

# Upper bound on how many child URLs one feed / sitemap expands into (ADR-0009 §2
# "bounded count"). A larger feed is truncated, not crawled to exhaustion.
MAX_CHILD_URLS = 50

# Tags whose text content is never readable prose — dropped wholesale. ``head`` is
# intentionally NOT here: the page ``<title>`` lives inside it and is captured
# separately; everything else in ``<head>`` (meta/link) emits no text data anyway.
_NON_CONTENT_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})

# Collapse runs of whitespace (incl. newlines) introduced by tag boundaries.
_WS_RUN = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n\s*\n\s*")


class _TextExtractor(HTMLParser):
    """Accumulate readable text, skipping script/style/etc. and the ``<title>``.

    Block-level tags emit a newline so paragraphs/headings don't run together;
    non-content subtrees are skipped entirely. The page ``<title>`` is captured
    separately (it names the resulting document).
    """

    _BLOCK_TAGS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _NON_CONTENT_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _NON_CONTENT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
            return
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _normalize_ws(text: str) -> str:
    """Collapse intra-line whitespace runs and blank-line runs; strip the ends."""
    collapsed = _WS_RUN.sub(" ", text)
    collapsed = _BLANK_LINES.sub("\n\n", collapsed)
    return "\n".join(line.strip() for line in collapsed.splitlines()).strip()


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """The readable text + title extracted from one HTML (or text) page."""

    title: str
    text: str


def extract_page_text(content: str, *, content_type: str) -> ExtractedPage:
    """Extract readable text + a title from a fetched page.

    For ``text/html`` (and xhtml) runs the HTML-to-text extractor; for
    ``text/plain`` returns the text as-is (first non-blank line as a title).
    Whitespace is normalised either way.
    """
    bare = content_type.split(";", 1)[0].strip().lower()
    if bare in {"text/html", "application/xhtml+xml"}:
        parser = _TextExtractor()
        parser.feed(content)
        parser.close()
        return ExtractedPage(
            title=_normalize_ws(parser.title),
            text=_normalize_ws(parser.text()),
        )
    text = _normalize_ws(content)
    title = next((line for line in text.splitlines() if line.strip()), "")
    return ExtractedPage(title=title, text=text)


def _localname(tag: str) -> str:
    """Strip an XML namespace (``{ns}local`` → ``local``), lowercased."""
    return tag.rsplit("}", 1)[-1].lower()


def _try_parse_xml(content: str) -> ET.Element | None:
    """Parse ``content`` as XML, returning the root, or ``None`` if it isn't XML."""
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One entry of an RSS/Atom feed — its title + link (and inline summary)."""

    title: str
    url: str
    summary: str


def parse_feed(content: str) -> list[FeedItem] | None:
    """Parse RSS or Atom ``content`` into bounded :class:`FeedItem`s, or ``None``.

    Returns ``None`` when ``content`` is not a recognised feed (the caller then
    treats the resource as a single page). RSS ``<item>`` and Atom ``<entry>``
    are both handled; the per-item link is the RSS ``<link>`` text or the Atom
    ``<link href>``. Capped at :data:`MAX_CHILD_URLS` (ADR-0009 §2 bounded count).
    """
    root = _try_parse_xml(content)
    if root is None:
        return None
    root_name = _localname(root.tag)

    items: list[FeedItem] = []
    if root_name == "rss":
        entries = _iter_local(root, "item")
    elif root_name == "feed":
        entries = _iter_local(root, "entry")
    else:
        return None

    for entry in entries:
        title = _child_text(entry, "title")
        summary = _child_text(entry, "description") or _child_text(entry, "summary")
        link = _feed_link(entry)
        if link:
            items.append(
                FeedItem(
                    title=_normalize_ws(title),
                    url=link,
                    summary=_normalize_ws(summary),
                )
            )
        if len(items) >= MAX_CHILD_URLS:
            break
    return items


def parse_sitemap(content: str) -> list[str] | None:
    """Parse a sitemap.xml (or sitemap index) into a bounded URL list, or ``None``.

    Handles both ``<urlset>`` (page URLs) and ``<sitemapindex>`` (child sitemaps);
    in both cases the ``<loc>`` texts are collected. Returns ``None`` when
    ``content`` is not a sitemap. Capped at :data:`MAX_CHILD_URLS`.
    """
    root = _try_parse_xml(content)
    if root is None:
        return None
    root_name = _localname(root.tag)
    if root_name not in {"urlset", "sitemapindex"}:
        return None
    locs: list[str] = []
    for loc in root.iter():
        if _localname(loc.tag) == "loc" and loc.text and loc.text.strip():
            locs.append(loc.text.strip())
            if len(locs) >= MAX_CHILD_URLS:
                break
    return locs


def _iter_local(parent: ET.Element, name: str) -> list[ET.Element]:
    """All descendants of ``parent`` whose local tag name is ``name`` (ns-agnostic)."""
    return [el for el in parent.iter() if _localname(el.tag) == name]


def _child_text(parent: ET.Element, name: str) -> str:
    """The text of ``parent``'s first child with local name ``name`` (or "")."""
    for child in parent:
        if _localname(child.tag) == name and child.text:
            return child.text
    return ""


def _feed_link(entry: ET.Element) -> str:
    """Resolve an item/entry link: RSS ``<link>`` text or Atom ``<link href>``.

    Atom prefers a ``rel="alternate"`` (or unspecified rel) link; an ``href`` is
    required. RSS carries the URL as the ``<link>`` element's text.
    """
    for child in entry:
        if _localname(child.tag) != "link":
            continue
        href = child.get("href")
        if href:  # Atom
            rel = (child.get("rel") or "alternate").lower()
            if rel == "alternate":
                return href.strip()
        elif child.text and child.text.strip():  # RSS
            return child.text.strip()
    # Fall back to the first Atom link with any rel if no alternate was found.
    for child in entry:
        if _localname(child.tag) == "link":
            href = child.get("href")
            if href:
                return href.strip()
    return ""


__all__ = [
    "MAX_CHILD_URLS",
    "ExtractedPage",
    "FeedItem",
    "extract_page_text",
    "parse_feed",
    "parse_sitemap",
]
