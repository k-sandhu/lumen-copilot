"""Optional offline comparison against the pure Rust fixture driver."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.ingestion.parsers import parse_document
from tests import test_ingestion_docx_tables as fixtures

MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize(
    "case",
    [
        name
        for name in vars(fixtures)
        if name.startswith("test_docx_")
        and not any(term in name for term in ("budget", "corrupt", "depth"))
    ],
)
def test_python_docx_fixture_parity(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    driver = os.environ.get("DOCINTEL_DOCX_DRIVER")
    if not driver:
        pytest.skip("build Rust docx_extract and set DOCINTEL_DOCX_DRIVER")

    def compare(data: bytes, *, mime_type: str) -> str:
        expected = parse_document(data, mime_type=mime_type)
        source = tmp_path / "generated.docx"
        source.write_bytes(data)
        result = subprocess.run(
            [driver, str(source)], check=True, capture_output=True, text=True, encoding="utf-8"
        )
        rendered = json.loads(result.stdout)
        blocks = rendered["document"]["blocks"]
        # Renderer v1 joins canonical blocks with two newlines; Python uses one.
        actual = "\n".join(block["text"] for block in blocks)
        assert actual == expected
        for span, block in zip(rendered["spans"], blocks, strict=True):
            assert rendered["rendered_text"][span["char_start"] : span["char_end"]] == block["text"]
        return actual

    monkeypatch.setattr(fixtures, "parse_document", compare)
    getattr(fixtures, case)()
