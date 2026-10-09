"""#664: exact Unicode spans, native metadata and current-parser text parity."""

from __future__ import annotations

import json

import pytest

from app.ingestion.parsers import parse_document
from tests.test_ingestion_parsers import _make_docx, _make_pdf, _make_pptx, _make_xlsx


def draft(text: str) -> str:
    return json.dumps(
        {"schema_version": 1, "renderer_version": 1, "blocks": [{"id": "b0", "text": text}]}
    )


def test_native_model_typed_provenance_roundtrip() -> None:
    pytest.importorskip("lumen_docintel")
    from app.ingestion.native import render_canonical

    data = json.loads(draft("A😀é\nZ"))
    data["blocks"].append({"id": "b1", "text": "尾", "parent_id": "b0"})
    data["blocks"][0]["regions"] = [
        {
            "kind": "page",
            "number": 2,
            "bbox": {
                "x0": 1.0,
                "y0": 2.0,
                "x1": 3.0,
                "y1": 4.0,
                "unit": "point",
                "origin": "top_left",
            },
        }
    ]
    data["source_parts"] = [
        {"kind": "page", "name": "Page 2", "number": 2, "char_start": 0, "char_end": 5}
    ]
    data["generation"] = {
        "fingerprint": {"schema_version": 1, "embedding": None},
        "outcome": "partial",
        "diagnostics": {"warning_codes": ["blank_part"]},
    }
    result = render_canonical(json.dumps(data))
    assert result.rendered_text == "A😀é\nZ\n\n尾"
    assert result.spans[0].char_end == 5
    assert result.blocks[0].regions[0].number == 2
    for span, block in zip(result.spans, result.blocks, strict=True):
        assert result.rendered_text[span.char_start : span.char_end] == block.text
    assert render_canonical(result.document_json) == result


@pytest.mark.parametrize(
    "data,mime",
    [
        (b"A\xf0\x9f\x98\x80\xc3\xa9", "text/plain"),
        (b"# Heading\nbody", "text/markdown"),
        (_make_pdf("PDF fact"), "application/pdf"),
        (
            _make_docx("DOCX fact"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            _make_pptx("PPTX fact"),
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        (
            _make_xlsx("XLSX fact"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
    ids=["txt", "markdown", "pdf", "docx", "pptx", "xlsx"],
)
def test_current_parser_exact_text_model_parity(data: bytes, mime: str) -> None:
    pytest.importorskip("lumen_docintel")
    from app.ingestion.native import render_canonical

    baseline = parse_document(data, mime_type=mime)
    rendered = render_canonical(draft(baseline))
    assert rendered.rendered_text == baseline
    assert (
        rendered.rendered_text[rendered.spans[0].char_start : rendered.spans[0].char_end]
        == baseline
    )


def test_model_rejects_cyclic_or_malformed_input() -> None:
    native = pytest.importorskip("lumen_docintel")
    from app.ingestion.native import render_canonical

    with pytest.raises(native.DocIntelInvalidInputError):
        render_canonical('{"blocks":[{"id":"a","parent_id":"a","text":"secret"}]}')
    with pytest.raises(native.DocIntelInvalidInputError):
        render_canonical("invalid json")
