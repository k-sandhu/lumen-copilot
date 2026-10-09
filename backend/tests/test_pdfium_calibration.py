"""Generated budget regressions; no external source bytes or identities."""

from __future__ import annotations

import json

import pytest

from app.domain.native_runtime import RuntimeBudget
from tests.eval.docintel.pdf_fixtures import document, text


@pytest.mark.parametrize(
    ("override", "limit"),
    [
        ({"max_memory_bytes": 128}, "memory"),
        ({"max_work_units": 1}, "work"),
        ({"max_output_chars": 1}, "output"),
    ],
)
def test_worker_failure_preserves_only_safe_budget_counters(
    override: dict[str, int],
    limit: str,
) -> None:
    pytest.importorskip("lumen_docintel")
    from app.ingestion.native import PdfiumExecutor, PdfWorkerError

    with pytest.raises(PdfWorkerError) as caught:
        PdfiumExecutor().extract_pdf(
            document([text(40, 700, 12, "PrivateSentinel")]),
            budget=RuntimeBudget(**override),
        )
    assert caught.value.code == "budget"
    assert caught.value.diagnostics["limit"] == limit
    assert "PrivateSentinel" not in json.dumps(caught.value.diagnostics)
    assert set(caught.value.diagnostics) == {
        "limit",
        "pages",
        "glyphs",
        "peak_accounted_bytes",
        "work_units",
        "output_chars",
        "source_input_bytes",
    }


def test_long_document_does_not_charge_dead_page_scratch_as_retained_memory() -> None:
    pytest.importorskip("lumen_docintel")
    from app.ingestion.native import PdfiumExecutor

    content = b"\n".join(
        text(40, 740 - row * 14, 10, "Generated evidence " * 4) for row in range(45)
    )
    result = PdfiumExecutor(memory_cap_bytes=512 * 1024 * 1024).extract_pdf(
        document([content] * 100),
        budget=RuntimeBudget(max_memory_bytes=256 * 1024 * 1024, max_work_units=5_000_000),
    )
    assert json.loads(result.generation_json)["outcome"] == "indexed"
    assert len(json.loads(result.document_json)["source_parts"]) == 100
