"""Offline opt-in admission, cutover and shadow negatives (#675)."""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.ingestion import native
from app.ingestion.parsers import DocumentParseError, parse_document


def test_csv_gate_and_unavailable_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: False)
    settings = Settings(native_csv_enabled=True)
    assert "text/csv" not in settings.effective_upload_content_types
    with pytest.raises(DocumentParseError):
        parse_document(b"A,B\n1,2", mime_type="text/csv", settings=settings)
    monkeypatch.setattr(native, "native_available", lambda: True)
    assert "text/csv" in settings.effective_upload_content_types
    assert "text/csv" not in Settings().effective_upload_content_types
    assert (
        "text/csv"
        not in Settings(upload_allowed_content_types="text/csv").effective_upload_content_types
    )


def test_csv_cutover_and_content_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: True)
    monkeypatch.setattr(
        native,
        "extract_candidate",
        lambda *args, **kwargs: SimpleNamespace(rendered_text="Row 1: A,B"),
    )
    monkeypatch.setattr(
        native, "detect_content", lambda *args, **kwargs: SimpleNamespace(format="csv")
    )
    assert (
        parse_document(
            b"A,B\n1,2", mime_type="text/csv", settings=Settings(native_csv_enabled=True)
        )
        == "Row 1: A,B"
    )
    monkeypatch.setattr(
        native, "detect_content", lambda *args, **kwargs: SimpleNamespace(format="pdf")
    )
    with pytest.raises(DocumentParseError):
        parse_document(
            b"%PDF-1.7", mime_type="text/csv", settings=Settings(native_csv_enabled=True)
        )


def test_csv_shadow_alone_does_not_admit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "native_available", lambda: True)
    assert "text/csv" not in Settings(native_csv_shadow=True).effective_upload_content_types
