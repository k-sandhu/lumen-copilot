"""Replay every generated workbook baseline case through the optional Rust driver."""
from __future__ import annotations

import itertools
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.ingestion.parsers import DocumentParseError, parse_document
from tests import test_ingestion_workbooks as fixtures


def _cases() -> list[tuple[str, dict[str, object]]]:
    cases = []
    for name, function in vars(fixtures).items():
        if not name.startswith("test_"):
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
def test_python_workbook_fixture_parity(
    case: str, kwargs: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    driver = os.environ.get("DOCINTEL_WORKBOOK_DRIVER")
    if not driver:
        pytest.skip("build workbook_extract and set DOCINTEL_WORKBOOK_DRIVER")

    def compare(data: bytes, *, mime_type: str) -> str:
        source = tmp_path / "generated.xlsx"
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
        assert rendered["rendered_text"] == expected
        for span, block in zip(rendered["spans"], rendered["document"]["blocks"], strict=True):
            assert expected[span["char_start"] : span["char_end"]] == block["text"]
        return expected

    monkeypatch.setattr(fixtures, "parse_document", compare)
    getattr(fixtures, case)(**kwargs)
