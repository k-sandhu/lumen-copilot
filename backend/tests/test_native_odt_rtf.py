"""Generated fixtures and default-OFF routing for #677, without network access."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from app.core.config import Settings
from app.ingestion import native


def test_disabled_candidate_never_imports_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> None:
        raise AssertionError("disabled candidate imported extension")

    monkeypatch.setattr(native, "_extension", forbidden)
    for fmt in ("odt", "rtf"):
        with pytest.raises(native.NativeFormatDisabledError):
            native.extract_open_document(b"untrusted", format_name=fmt, settings=Settings())


def test_shadow_error_preserves_python_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("private payload must not appear")

    monkeypatch.setattr(native, "extract_open_document", fail)
    settings = Settings(native_rtf_accept=True, native_rtf_shadow=True)
    result, diagnostics = native.shadow_open_document(
        b"untrusted", format_name="rtf", python_text="retained Python", settings=settings
    )
    assert result == "retained Python"
    assert diagnostics == {"format": "rtf", "candidate_outcome": "error"}


def test_promotion_and_unknown_formats_stay_closed() -> None:
    settings = Settings(native_rtf_accept=True, native_rtf_cutover=True)
    assert native.open_document_route("rtf", settings=settings) == "promotion_pending"
    assert native.open_document_route("odt", settings=settings) == "unsupported"
    assert native.open_document_route("doc", settings=settings) == "unsupported"


def test_actual_bridge_generated_odt_rtf() -> None:
    extension = pytest.importorskip("lumen_docintel")
    if not hasattr(extension, "extract_open_document"):
        pytest.fail("native extra must be rebuilt for #677")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        package.writestr(
            "content.xml",
            '<office:document-content '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            "<office:body><office:text><text:p>café 😀</text:p></office:text></office:body>"
            "</office:document-content>",
        )
    settings = Settings(native_odt_accept=True, native_rtf_accept=True)
    for fmt, source in (
        ("odt", buffer.getvalue()),
        ("rtf", rb"{\rtf1\ansi\uc1 caf\'e9 \u-10179?\u-8704?}"),
    ):
        result = native.extract_open_document(source, format_name=fmt, settings=settings)
        assert result.rendered_text == "café 😀"
        span = result.spans[0]
        assert result.rendered_text[span.char_start : span.char_end] == result.blocks[0].text
        assert native.detect_content(source).format == fmt


def test_configuration_defaults_and_invalid_rtf() -> None:
    settings = Settings()
    for fmt in ("odt", "rtf"):
        for flag in ("accept", "shadow", "cutover"):
            assert getattr(settings, f"native_{fmt}_{flag}") is False
    extension = pytest.importorskip("lumen_docintel")
    if not hasattr(extension, "extract_open_document"):
        pytest.fail("native extra must be rebuilt for #677")
    with pytest.raises(extension.DocIntelParseError):
        native.extract_open_document(
            rb"{\rtf1 unclosed", format_name="rtf", settings=Settings(native_rtf_accept=True)
        )
