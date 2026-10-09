"""Probe privacy and failure denominators use only test-generated inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.eval.docintel.pdf_fixtures import document, text
from tests.eval.docintel.pdfium_fixtures import object_stream


def test_aggregate_probe_has_no_input_identities_or_contents(tmp_path: Path) -> None:
    pytest.importorskip("lumen_docintel")
    from tests.eval.docintel.pdf_corpus_probe import MAX_BYTES, run

    (tmp_path / "private-sentinel-name.bin").write_bytes(
        document([text(40, 700, 12, "PrivateSentinelContent")])
    )
    (tmp_path / "modern.anything").write_bytes(object_stream())
    (tmp_path / "broken").write_bytes(b"%PDF-1.7\nnot a document")
    (tmp_path / "ignore.pdf").write_bytes(b"not pdf")
    with (tmp_path / "oversized").open("wb") as source:
        source.write(b"%PDF-")
        source.truncate(MAX_BYTES + 1)
    report = run(tmp_path, workers=1)
    encoded = json.dumps(report)
    assert "private-sentinel" not in encoded
    assert "PrivateSentinel" not in encoded
    assert str(tmp_path) not in encoded
    assert "sha256" not in encoded
    assert report["pdf_documents"] == 3
    assert report["skipped_over_100_mib"] == 1
    for engine in report["engines"].values():
        assert (
            sum(
                engine[key] for key in ("parsed", "needs_ocr", "unsupported", "failed", "timed_out")
            )
            == 3
        )
    assert report["engines"]["pdfium"]["parsed"] == 2
    assert report["engines"]["in_core"]["unsupported"] == 1
    assert report["engines"]["pypdf"]["pages"] == 2
