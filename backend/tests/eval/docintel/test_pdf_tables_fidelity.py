"""#672 cell associations and non-table false-positive denominator."""

from __future__ import annotations

import base64
from dataclasses import asdict

import pytest

from tests.eval.docintel.benchmark import _child
from tests.eval.docintel.metrics import Score, require_fidelity
from tests.eval.docintel.pdf_table_fixtures import corpus


@pytest.mark.parametrize("arm", ["native-extraction", "pdfium-extraction"])
def test_pdf_table_fidelity_and_explicit_false_positive_denominator(arm: str) -> None:
    pytest.importorskip("lumen_docintel")
    negatives = 0
    false_positives = 0
    for fixture in corpus():
        result = _child(
            {
                "data": base64.b64encode(fixture.data).decode(),
                "mime": fixture.mime,
                "gold": asdict(fixture.gold),
                "arm": arm,
            }
        )
        require_fidelity(Score(**result["score"]), format_name="pdf")
        if fixture.case == "non-table-prose":
            negatives += 1
            false_positives += result["table_count"] > 0
        else:
            assert result["table_count"] == 1
            assert result["score"]["cell_header_association"] == 1.0
    assert negatives == 4
    assert false_positives == 0
