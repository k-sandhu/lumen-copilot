"""Offline EPUB admission and generated fixture fidelity (#681)."""

import pytest

from app.core.config import Settings
from app.ingestion import native
from app.ingestion.parsers import DocumentParseError, parse_document


def test_epub_independent_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: True)
    monkeypatch.setattr(native, "candidate_available", lambda family: True)
    assert (
        "application/epub+zip"
        not in Settings(native_html_enabled=True).effective_upload_content_types
    )
    assert (
        "application/epub+zip"
        not in Settings(native_epub_shadow=True).effective_upload_content_types
    )
    assert (
        "application/epub+zip" in Settings(native_epub_enabled=True).effective_upload_content_types
    )
    with pytest.raises(DocumentParseError):
        parse_document(b"x", mime_type="application/epub+zip", settings=Settings())


def test_epub_fixture_fidelity() -> None:
    pytest.importorskip("lumen_docintel")
    from tests.eval.docintel.fixtures import corpus
    from tests.eval.docintel.metrics import evaluate

    fixture = next(f for f in corpus() if f.format == "epub")
    doc = native.extract_candidate(fixture.data, family="epub", mime=fixture.mime)
    spans = tuple(
        (s.char_start, s.char_end, b.text) for s, b in zip(doc.spans, doc.blocks, strict=True)
    )
    score = evaluate(doc.rendered_text, fixture.gold, spans=spans)
    assert score.fact_coverage == score.reading_order == score.exact_offsets == 1
