"""#670: losses remain visible per format and failures stay in denominators."""
from __future__ import annotations

import pytest

from tests.eval.docintel.metrics import Gold, evaluate, require_fidelity


def test_dropped_table_page_or_sheet_fails_its_format_gate() -> None:
    gold = Gold(facts=("Intro", "North", "-120", "kg", "SheetTwo"),
                associations=(("North", "-120", "kg"),),
                order=("Intro", "North", "SheetTwo"))
    for output in ["Intro\nSheetTwo", "Intro\nNorth -120 kg", "SheetTwo\nNorth -120 kg\nIntro"]:
        with pytest.raises(ValueError):
            require_fidelity(evaluate(output, gold), format_name="xlsx")


def test_wrong_sign_or_value_does_not_count_as_covered_fact() -> None:
    score = evaluate("North 1200 kg", Gold(facts=("North", "120", "kg")))
    assert score.fact_coverage == 2 / 3
    score = evaluate("North -120 kg", Gold(facts=("North", "120", "kg")))
    assert score.fact_coverage == 2 / 3


def test_corrupt_offsets_and_failed_documents_cannot_pass() -> None:
    gold = Gold(facts=("A😀",))
    with pytest.raises(ValueError):
        require_fidelity(evaluate("A😀", gold, spans=((0,1,"A😀"),)), format_name="text")
    with pytest.raises(ValueError):
        require_fidelity(evaluate("", gold, outcome="failed"), format_name="text")


def test_generated_corpus_is_deterministic_and_covers_families() -> None:
    from tests.eval.docintel.fixtures import corpus
    first = corpus()
    second = corpus()
    assert [(f.id,f.sha256) for f in first] == [(f.id,f.sha256) for f in second]
    assert {f.format for f in first} >= {"pdf","docx","pptx","xlsx","odt","ods","odp",
        "epub","zip","tar","gzip","csv","tsv","json","jsonl","xml","xbrl","ixbrl",
        "ipynb","eml","mbox","mhtml","rtf","text","markdown","doc","xls","ppt","msg","image"}
