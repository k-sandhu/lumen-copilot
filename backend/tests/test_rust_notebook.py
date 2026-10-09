"""Offline notebook admission, inert outputs and incomplete-cutover rejection."""

import json

import pytest

from app.core.config import Settings
from app.ingestion.parsers import DocumentParseError, parse_document


def test_notebook_candidate() -> None:
    pytest.importorskip("lumen_docintel")
    from app.ingestion import native

    mime = "application/x-ipynb+json"
    data = json.dumps(
        {
            "nbformat": 4,
            "cells": [
                {
                    "cell_type": "code",
                    "source": "raise RuntimeError('inert')",
                    "outputs": [{"output_type": "stream", "text": "東京"}],
                }
            ],
        }
    ).encode()
    assert mime not in Settings().effective_upload_content_types
    settings = Settings(native_notebook_enabled=True)
    assert mime in settings.effective_upload_content_types
    assert "東京" in parse_document(data, mime_type=mime, settings=settings)
    with pytest.raises(DocumentParseError):
        parse_document(data, mime_type=mime)
    with pytest.raises(DocumentParseError):
        parse_document(b"{broken", mime_type=mime, settings=settings)
    oversized = json.dumps(
        {
            "nbformat": 4,
            "cells": [
                {
                    "cell_type": "code",
                    "source": "x",
                    "outputs": [{"output_type": "stream", "text": "x" * (1024 * 1024 + 1)}],
                }
            ],
        }
    ).encode()
    doc = native.extract_candidate(oversized, family="notebook", mime=mime)
    assert json.loads(doc.generation_json)["outcome"] == "partial"
    with pytest.raises(DocumentParseError):
        parse_document(oversized, mime_type=mime, settings=settings)
