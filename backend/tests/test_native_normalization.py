from __future__ import annotations

import json

import pytest

from app.ingestion.native import normalize_canonical, render_canonical


def test_native_normalization_preserves_evidence_and_records_diagnostics() -> None:
    pytest.importorskip("lumen_docintel")
    source = "ofﬁce A-123 12.50 kg infor-\nmation"
    document = render_canonical(json.dumps({"blocks": [{"id": "b", "text": source}]}))
    result = normalize_canonical(document)
    assert result.document.rendered_text == source
    assert result.blocks[0].text.startswith("office")
    assert "A-123 12.50 kg infor-\nmation" in result.blocks[0].text
    generation = json.loads(result.document.generation_json)
    assert "native_normalizer" in generation["fingerprint"]
    diagnostics = json.loads(result.diagnostics_json)
    assert diagnostics["outcome"] == "text_present"
    assert "ambiguous_hyphenation" in diagnostics["warnings"]
