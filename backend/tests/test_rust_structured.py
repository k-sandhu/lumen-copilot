"""Offline independent structured candidate cutover and typed security rejection."""

import json

import pytest

from app.core.config import Settings
from app.ingestion import native
from app.ingestion.parsers import DocumentParseError, parse_document


@pytest.mark.parametrize("family", ["json", "jsonl", "xml", "xbrl", "ixbrl"])
def test_generated_fidelity_and_cutover(family: str) -> None:
    pytest.importorskip("lumen_docintel")
    from tests.eval.docintel.fixtures import corpus
    from tests.eval.docintel.metrics import evaluate

    fixture = next(f for f in corpus() if f.format == family)
    doc = native.extract_candidate(fixture.data, family=family, mime=fixture.mime)
    spans = tuple(
        (s.char_start, s.char_end, b.text) for s, b in zip(doc.spans, doc.blocks, strict=True)
    )
    score = evaluate(doc.rendered_text, fixture.gold, spans=spans)
    assert (
        score.fact_coverage
        == score.table_association
        == score.reading_order
        == score.exact_offsets
        == 1
    )
    assert fixture.mime not in Settings().effective_upload_content_types
    settings = Settings(**{f"native_{family}_enabled": True})
    assert fixture.mime in settings.effective_upload_content_types
    assert (
        parse_document(fixture.data, mime_type=fixture.mime, settings=settings) == doc.rendered_text
    )
    with pytest.raises(DocumentParseError):
        parse_document(fixture.data, mime_type=fixture.mime)


def test_entities_depth_and_transformation_are_blocked() -> None:
    pytest.importorskip("lumen_docintel")
    for hostile in [
        b"<!DOCTYPE r SYSTEM 'file:///secret'><r/>",
        b"<!DOCTYPE r [<!ENTITY a 'x'>]><r>&a;</r>",
        b"<r>&custom;</r>",
    ]:
        with pytest.raises(DocumentParseError):
            parse_document(
                hostile, mime_type="application/xml", settings=Settings(native_xml_enabled=True)
            )
    deep = b"[" * 70 + b"0" + b"]" * 70
    with pytest.raises(DocumentParseError):
        parse_document(
            deep, mime_type="application/json", settings=Settings(native_json_enabled=True)
        )
    inline = (
        b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        b'<ix:nonFraction name="Mass" contextRef="C" format="unsupported">'
        b"1,234</ix:nonFraction></html>"
    )
    excluded = (
        b'<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        b'<ix:nonFraction name="Mass" contextRef="C">120'
        b"<ix:exclude>annotation</ix:exclude></ix:nonFraction></html>"
    )
    for data in (inline, excluded):
        doc = native.extract_candidate(data, family="ixbrl", mime="application/ixbrl+xml")
        assert json.loads(doc.generation_json)["outcome"] == "partial"
        with pytest.raises(DocumentParseError):
            parse_document(
                data,
                mime_type="application/ixbrl+xml",
                settings=Settings(native_ixbrl_enabled=True),
            )


def test_shadow_and_old_wheel_keep_admission_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "candidate_available", lambda family: False)
    settings = Settings(native_json_enabled=True, native_xml_shadow=True)
    assert "application/json" not in settings.effective_upload_content_types
    with pytest.raises(DocumentParseError):
        parse_document(b"{}", mime_type="application/json", settings=settings)


def test_numeric_precision_survives_python_view() -> None:
    pytest.importorskip("lumen_docintel")
    data = b'{"integer":18446744073709551617,"decimal":0.12345678901234567890123456789}'
    doc = native.extract_candidate(data, family="json", mime="application/json")
    fields = json.loads(doc.generation_json)["diagnostics"]["records"][0]["fields"]
    assert fields[0]["value"] == 18446744073709551617
    for field, expected in zip(
        fields, ("18446744073709551617", "0.12345678901234567890123456789"), strict=True
    ):
        assert field["number_text"] == expected
        assert expected in doc.rendered_text
