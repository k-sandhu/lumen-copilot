"""Offline PDF bridge and per-format shadow behavior (#671)."""
from __future__ import annotations

import pytest
from tests.eval.docintel.fixtures import _pdf


def test_pdf_bridge_page_provenance_unicode_and_typed_limits() -> None:
    extension = pytest.importorskip("lumen_docintel")
    from app.ingestion.native import NativeExecutor
    from app.domain.native_runtime import RuntimeBudget
    executor = NativeExecutor()
    document = executor.extract_pdf(_pdf())
    assert len(document.source_parts) == 3
    assert document.generation.outcome == "partial"
    for block, span in zip(document.blocks, document.spans, strict=True):
        assert document.rendered_text[span.char_start:span.char_end] == block.text
        assert block.regions[0].bbox is not None
    with pytest.raises(extension.DocIntelBudgetError):
        executor.extract_pdf(_pdf(), budget=RuntimeBudget(max_memory_bytes=128))
    assert "Intro" in executor.extract_pdf(_pdf()).rendered_text


def test_pdf_shadow_failure_preserves_python_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ingestion import native
    monkeypatch.setattr(native, "_extension", lambda: None)
    result = native.parse_pdf_candidate(_pdf(), mode="shadow")
    assert "Intro" in result.text
    assert result.route == "python"
    assert result.native_error == "native_unavailable"


def test_pdf_defaults_to_python_and_invalid_mode_is_rejected() -> None:
    from app.ingestion.native import parse_pdf_candidate
    result = parse_pdf_candidate(_pdf())
    assert result.route == "python"
    assert result.canonical is None
    with pytest.raises(ValueError):
        parse_pdf_candidate(_pdf(), mode="anything")  # type: ignore[arg-type]


def test_pdf_shadow_needs_ocr_is_visible_and_not_promoted() -> None:
    pytest.importorskip("lumen_docintel")
    from app.ingestion.native import parse_pdf_candidate
    result = parse_pdf_candidate(_pdf(scanned=True), mode="shadow")
    assert result.route == "python"
    assert result.canonical is not None
    assert result.canonical.generation.outcome == "needs_ocr"
