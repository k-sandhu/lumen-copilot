"""#665: content, encoding, typed unsupported and parser gating."""

from __future__ import annotations

import pytest


def test_content_detection_and_encoding() -> None:
    pytest.importorskip("lumen_docintel")
    from app.ingestion.native import detect_content, plan_native_route

    result = detect_content(b"%PDF-1.4\n", declared_mime="text/plain")
    assert result.format == "pdf" and result.declared_mismatch
    assert plan_native_route(result, enabled_formats=frozenset({"pdf"})) == "python_fallback"
    text = detect_content("Hello é Ω 😀".encode("utf-16-le"))
    assert text.decoded_text == "Hello é Ω 😀"
    assert text.decoding_errors == 0
    csv = detect_content(b"A,B\n1,2\n3,4")
    assert csv.format == "csv"
    assert plan_native_route(csv, enabled_formats=frozenset({"csv"})) == "unsupported"


def test_unknown_binary_and_corrupt_container_are_typed() -> None:
    native = pytest.importorskip("lumen_docintel")
    from app.ingestion.native import detect_content

    with pytest.raises(native.DocIntelUnsupportedError):
        detect_content(b"\0\xff\0\xad\x11")
    with pytest.raises(native.DocIntelParseError):
        detect_content(b"PK\x03\x04broken")
