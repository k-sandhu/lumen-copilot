"""Native PDF fidelity must beat structural losses, not just render controls."""

from __future__ import annotations

import base64
from dataclasses import asdict

import pytest

from tests.eval.docintel.benchmark import _child
from tests.eval.docintel.metrics import Score, require_fidelity
from tests.eval.docintel.pdf_fixtures import corpus


@pytest.mark.parametrize("arm", ["native-extraction", "pdfium-extraction"])
def test_generated_layout_gold_passes_native_gate_and_baseline_loses_order(arm: str) -> None:
    pytest.importorskip("lumen_docintel")
    fixture = corpus()[0]
    payload = {
        "data": base64.b64encode(fixture.data).decode(),
        "mime": fixture.mime,
        "gold": asdict(fixture.gold),
        "arm": arm,
    }
    native = _child(payload)
    require_fidelity(Score(**native["score"]), format_name="pdf")
    payload["arm"] = "python-provenance-baseline"
    baseline = _child(payload)
    assert baseline["score"]["fact_coverage"] == 1.0
    assert baseline["score"]["reading_order"] == 2 / 3
    assert baseline["score"]["native_provenance"] == 1.0
    with pytest.raises(ValueError):
        require_fidelity(Score(**baseline["score"]), format_name="pdf")
