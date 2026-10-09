"""Generated offline coverage and process containment for #722."""

from __future__ import annotations

import json

import pytest

from tests.eval.docintel.pdf_fixtures import document, text


def test_pdfium_generated_text_and_exact_spans() -> None:
    from app.ingestion.native import PdfiumExecutor

    executor = PdfiumExecutor()
    result = executor.extract_pdf(document([text(40, 700, 12, "Worker text")]))
    assert "Worker text" in result.rendered_text
    assert json.loads(result.generation_json)["parser_id"] == "rust-pdfium"
    for block, span in zip(result.blocks, result.spans, strict=True):
        assert result.rendered_text[span.char_start : span.char_end] == block.text
        assert block.regions[0].bbox is not None


def test_pdfium_scan_is_typed_needs_ocr() -> None:
    from app.ingestion.native import PdfiumExecutor

    result = PdfiumExecutor().extract_pdf(document([b""]))
    assert json.loads(result.generation_json)["outcome"] == "needs_ocr"


def test_pdfium_malformed_is_contained_then_next_document_works() -> None:
    from app.ingestion.native import PdfiumExecutor, PdfWorkerError

    executor = PdfiumExecutor()
    with pytest.raises(PdfWorkerError) as failed:
        executor.extract_pdf(b"%PDF-1.7\nmalformed")
    assert failed.value.code == "parse_error"
    assert "Healthy" in executor.extract_pdf(document([text(40, 700, 12, "Healthy")])).rendered_text
