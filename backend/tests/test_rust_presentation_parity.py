"""Optional offline replay of the presentation baseline fixtures."""

from __future__ import annotations

import itertools
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.ingestion.parsers import DocumentParseError, parse_document
from tests import test_ingestion_slides as fixtures


def _cases() -> list[tuple[str, dict[str, object]]]:
    cases = []
    for name, function in vars(fixtures).items():
        if not name.startswith("test_") or "does_not_create_notes" in name:
            continue
        variants = [{}]
        for mark in getattr(function, "pytestmark", []):
            if mark.name != "parametrize":
                continue
            names, values = mark.args[:2]
            names = names.split(",") if isinstance(names, str) else names
            variants = [
                {**variant, **dict(zip(names, value if len(names) > 1 else [value], strict=True))}
                for variant, value in itertools.product(variants, values)
            ]
        cases.extend((name, variant) for variant in variants)
    return cases


@pytest.mark.parametrize("case,kwargs", _cases())
def test_python_slide_fixture_parity(
    case: str, kwargs: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    driver = os.environ.get("DOCINTEL_PRESENTATION_DRIVER")
    if not driver:
        pytest.skip("build presentation_extract and set DOCINTEL_PRESENTATION_DRIVER")

    def compare(data: bytes, *, mime_type: str) -> str:
        source = tmp_path / "generated.pptx"
        source.write_bytes(data)
        result = subprocess.run(
            [driver, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=40
        )
        try:
            expected = parse_document(data, mime_type=mime_type)
        except DocumentParseError:
            assert result.returncode != 0
            raise
        assert result.returncode == 0, result.stderr
        rendered = json.loads(result.stdout)
        blocks = rendered["document"]["blocks"]
        actual = "\n".join(b["text"] for b in blocks)
        assert actual == expected
        for span, block in zip(rendered["spans"], blocks, strict=True):
            assert rendered["rendered_text"][span["char_start"] : span["char_end"]] == block["text"]
        return actual

    monkeypatch.setattr(fixtures, "parse_document", compare)
    getattr(fixtures, case)(**kwargs)
