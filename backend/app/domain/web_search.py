"""Domain types for web-search results (ADR-0014, issue #219, risk:security).

The types the ``search_web/`` adapter returns to its callers (the ``web_search``
agent tool, and through it the chat runtime). Pure, frozen dataclasses — **no
vendor/response types, no framework imports** (backend/AGENTS.md: ``domain/`` is
pure; ADR-0004 adapter rule 1). Swapping SearXNG for a hosted API changes only
the mapping *inside* ``search_web/``; this domain type and every caller are
unchanged.

A web result is deliberately **distinct** from a corpus
:class:`~app.domain.retrieval.RetrievedPassage`: a retrieved passage is a
permitted chunk in the tenant's own corpus (``document_id`` + char offsets); a
web result is a public URL + the snippet the provider returned (and, only when a
page was actually fetched through the ``connectors/web/fetch.py`` SSRF
chokepoint, the extracted passage text). Keeping the two apart preserves INV-3
("cite only what was retrieved/fetched"): a URL that was listed but never fetched
carries no ``fetched_passage``, so the runtime can never cite its body as if it
had been read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    """One ranked internet-search result (ADR-0014 §2, contract-neutral).

    The single type that crosses the ``search_web/`` adapter boundary — no
    SearXNG (or, later, hosted-API) response object leaks upward. ``title`` /
    ``url`` / ``snippet`` come from the search provider's structured result;
    ``published_at`` is set only when the provider reports it. ``fetched_passage``
    is populated **only** when the result page was retrieved + extracted through
    the ``connectors/web/fetch.py`` SSRF chokepoint (the cite-worthy passage-level
    text) — otherwise ``None``, so a citation can distinguish "snippet only" from
    "page fetched" and INV-3 holds (cite what was fetched, nothing more).

    Invariant: ``url`` is an ``https`` (or ``http``) URL the adapter accepted; the
    result is transient (read live by the model), never ingested into the corpus.
    """

    title: str
    url: str
    snippet: str
    published_at: datetime | None = None
    fetched_passage: str | None = None


__all__ = ["WebSearchResult"]
