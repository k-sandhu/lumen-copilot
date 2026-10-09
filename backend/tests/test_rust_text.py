"""Offline text candidate parity and rollback (#680)."""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.ingestion import native
from app.ingestion.parsers import DocumentParseError, parse_document


def test_text_shadow_failure_preserves_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: False)
    assert (
        parse_document(
            b"baseline", mime_type="text/plain", settings=Settings(native_text_shadow=True)
        )
        == "baseline"
    )
    assert "text/x-python" not in Settings().effective_upload_content_types
    with pytest.raises(DocumentParseError):
        parse_document(b"x", mime_type="text/x-python", settings=Settings(native_text_enabled=True))


def test_text_cutover_and_binary_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: True)
    monkeypatch.setattr(native, "candidate_available", lambda family: True)
    monkeypatch.setattr(native, "detect_content", lambda *a, **k: SimpleNamespace(format="pdf"))
    with pytest.raises(DocumentParseError):
        parse_document(
            b"%PDF-1.7", mime_type="text/plain", settings=Settings(native_text_enabled=True)
        )
    assert "text/x-python" in Settings(native_text_enabled=True).effective_upload_content_types


def test_fixture_parity() -> None:
    pytest.importorskip("lumen_docintel")
    from tests.eval.docintel.fixtures import corpus
    from tests.eval.docintel.metrics import evaluate

    for fixture in corpus():
        if fixture.format not in {"text", "markdown"}:
            continue
        doc = native.extract_candidate(fixture.data, family="text", mime=fixture.mime)
        spans = tuple(
            (s.char_start, s.char_end, b.text) for s, b in zip(doc.spans, doc.blocks, strict=True)
        )
        score = evaluate(doc.rendered_text, fixture.gold, spans=spans)
        assert score.fact_coverage == score.reading_order == score.exact_offsets == 1
