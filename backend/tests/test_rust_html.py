"""Offline opt-in saved-page routing and generated fidelity (#679)."""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.ingestion import native
from app.ingestion.parsers import DocumentParseError, parse_document


def test_html_admission_requires_enabled_capable_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: True)
    monkeypatch.setattr(native, "candidate_available", lambda family: False)
    assert "text/html" not in Settings(native_html_enabled=True).effective_upload_content_types
    monkeypatch.setattr(native, "candidate_available", lambda family: True)
    assert "text/html" not in Settings(native_html_shadow=True).effective_upload_content_types
    assert "text/html" in Settings(native_html_enabled=True).effective_upload_content_types
    with pytest.raises(DocumentParseError):
        parse_document(b"<p>fact</p>", mime_type="text/html", settings=Settings())


def test_native_html_fixture_fidelity() -> None:
    pytest.importorskip("lumen_docintel")
    from tests.eval.docintel.fixtures import corpus
    from tests.eval.docintel.metrics import evaluate

    for fixture in corpus():
        if fixture.format not in {"html", "xhtml", "mhtml"}:
            continue
        doc = native.extract_candidate(fixture.data, family="html", mime=fixture.mime)
        spans = tuple(
            (s.char_start, s.char_end, b.text) for s, b in zip(doc.spans, doc.blocks, strict=True)
        )
        score = evaluate(doc.rendered_text, fixture.gold, spans=spans)
        assert score.fact_coverage == score.table_association == score.reading_order == 1
        assert score.exact_offsets == 1


def test_partial_candidate_is_not_live_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: True)
    monkeypatch.setattr(native, "candidate_available", lambda family: True)
    monkeypatch.setattr(native, "detect_content", lambda *a, **k: SimpleNamespace(format="html"))
    monkeypatch.setattr(
        native,
        "extract_candidate",
        lambda *a, **k: SimpleNamespace(
            rendered_text="prefix", generation_json='{"outcome":"partial"}'
        ),
    )
    with pytest.raises(DocumentParseError):
        parse_document(
            b"<p>prefix</p>", mime_type="text/html", settings=Settings(native_html_enabled=True)
        )
