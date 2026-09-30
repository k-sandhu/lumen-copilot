"""Web evidence has no fabricated corpus identity."""

from __future__ import annotations

import uuid

import pytest


def test_web_citation_rejects_non_http_url() -> None:
    from app.domain.chat import WebCitation

    with pytest.raises(ValueError):
        WebCitation(
            id=uuid.uuid4(), handle="W1", url="javascript:alert(1)", title="bad", snippet="x"
        )


def test_web_citation_preserves_the_visible_text_without_corpus_ids() -> None:
    from app.domain.chat import WebCitation

    citation = WebCitation(
        id=uuid.uuid4(),
        handle="W1",
        url="https://example.test/a",
        title="A",
        snippet="Visible text",
    )
    assert citation.snippet == "Visible text"
    assert not hasattr(citation, "document_id")
    assert not hasattr(citation, "chunk_id")
