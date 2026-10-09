"""#670: losses remain visible per format and failures stay in denominators."""

from __future__ import annotations

import pytest

from tests.eval.docintel.metrics import Gold, evaluate, require_fidelity


def test_dropped_table_page_or_sheet_fails_its_format_gate() -> None:
    gold = Gold(
        facts=("Intro", "North", "-120", "kg", "SheetTwo"),
        associations=(("North", "-120", "kg"),),
        order=("Intro", "North", "SheetTwo"),
    )
    for output in ["Intro\nSheetTwo", "Intro\nNorth -120 kg", "SheetTwo\nNorth -120 kg\nIntro"]:
        with pytest.raises(ValueError):
            require_fidelity(evaluate(output, gold), format_name="xlsx")


def test_wrong_sign_or_value_does_not_count_as_covered_fact() -> None:
    score = evaluate("North 1200 kg", Gold(facts=("North", "120", "kg")))
    assert score.fact_coverage == 2 / 3
    score = evaluate("North -120 kg", Gold(facts=("North", "120", "kg")))
    assert score.fact_coverage == 2 / 3
    score = evaluate("North −120 kg", Gold(facts=("North", "120", "kg")))
    assert score.fact_coverage == 2 / 3


def test_corrupt_offsets_and_failed_documents_cannot_pass() -> None:
    gold = Gold(facts=("A😀",))
    with pytest.raises(ValueError):
        require_fidelity(evaluate("A😀", gold, spans=((0, 1, "A😀"),)), format_name="text")
    with pytest.raises(ValueError):
        require_fidelity(evaluate("", gold, outcome="failed"), format_name="text")


def test_generated_corpus_is_deterministic_and_covers_families() -> None:
    from tests.eval.docintel.fixtures import corpus

    first = corpus()
    second = corpus()
    assert [(f.id, f.sha256) for f in first] == [(f.id, f.sha256) for f in second]
    assert {f.format for f in first} >= {
        "pdf",
        "docx",
        "pptx",
        "xlsx",
        "odt",
        "ods",
        "odp",
        "epub",
        "zip",
        "tar",
        "gzip",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "xml",
        "xbrl",
        "ixbrl",
        "ipynb",
        "eml",
        "mbox",
        "mhtml",
        "rtf",
        "text",
        "markdown",
        "doc",
        "xls",
        "ppt",
        "msg",
        "image",
    }


def test_baseline_arm_extracts_text_and_keeps_failed_rows() -> None:
    import base64

    from tests.eval.docintel.benchmark import _child

    gold = {"facts": ["North", "-120", "kg"], "associations": [], "order": [], "native_regions": 0}
    payload = {
        "data": base64.b64encode(b"North -120 kg").decode(),
        "gold": gold,
        "mime": "text/plain",
        "arm": "python-baseline",
    }
    result = _child(payload)
    assert result["outcome"] == "indexed"
    assert result["score"]["fact_coverage"] == 1.0
    assert result["peak_rss_bytes"] > 0
    payload["mime"] = "application/octet-stream"
    result = _child(payload)
    assert result["outcome"] == "unsupported"
    assert result["score"]["fact_coverage"] == 0.0


def test_cell_header_associations_need_the_correct_header() -> None:
    gold = Gold(facts=("North", "-120"), headers=(("Region", "North"), ("Mass", "-120")))
    from tests.eval.docintel.metrics import require_fidelity

    with pytest.raises(ValueError):
        require_fidelity(
            evaluate("North -120", gold, cell_headers=(("Mass", "North"),)), format_name="xlsx"
        )
    require_fidelity(
        evaluate("North -120", gold, cell_headers=(("Region", "North"), ("Mass", "-120"))),
        format_name="xlsx",
    )


def test_gold_tracks_blank_pdf_parts_and_nested_attachment_facts() -> None:
    from tests.eval.docintel.fixtures import corpus

    fixtures = {fixture.id: fixture for fixture in corpus()}
    assert fixtures["pdf-columns"].gold.native_regions == 3
    assert "AttachmentFact" in fixtures["zip-nested"].gold.facts
    assert "AttachmentFact" in fixtures["eml-facts"].gold.facts


def test_linux_peak_uses_current_address_space_high_water(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    from tests.eval.docintel.benchmark import _rss

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=999999)),
    )
    monkeypatch.setattr(
        Path, "open", lambda *args, **kwargs: io.StringIO("Name: python\nVmHWM: 42 kB\n")
    )
    assert _rss() == 42 * 1024


def test_wrong_native_page_identity_cannot_pass_with_a_perfect_count() -> None:
    gold = Gold(
        facts=("Intro", "PageTwo"),
        regions=(("page", 1, "Intro", None), ("page", 2, "PageTwo", None)),
    )
    text = "Intro\nPageTwo"
    swapped = (("page", 2, None, 0, 5), ("page", 1, None, 6, 13))
    with pytest.raises(ValueError):
        require_fidelity(
            evaluate(text, gold, source_parts=swapped, matched_regions=100), format_name="pdf"
        )
    correct = (("page", 1, None, 0, 5), ("page", 2, None, 6, 13))
    require_fidelity(evaluate(text, gold, source_parts=correct), format_name="pdf")
    with pytest.raises(ValueError):
        require_fidelity(
            evaluate(text, gold, source_parts=(("page", 1, None, 0, 100),)), format_name="pdf"
        )


def test_external_corpus_is_lazy_and_rejects_escape(tmp_path, monkeypatch) -> None:
    import json
    from pathlib import Path

    from tests.eval.docintel.benchmark import _external

    root = tmp_path / "corpus"
    root.mkdir()
    (root / "first.txt").write_text("Intro", encoding="utf-8")
    (root / "second.txt").write_text("PageTwo", encoding="utf-8")
    manifest = root / "manifest.json"
    rows = [
        {"path": name, "format": "text", "mime": "text/plain", "gold": {}}
        for name in ("first.txt", "second.txt")
    ]
    manifest.write_text(json.dumps(rows), encoding="utf-8")
    opened = []
    real_open = Path.open

    def observed(path, *args, **kwargs):
        opened.append(path.name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observed)
    iterator = iter(_external(manifest))
    assert next(iterator).data == b"Intro"
    assert "second.txt" not in opened
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    rows[0]["path"] = "../outside.txt"
    manifest.write_text(json.dumps(rows[:1]), encoding="utf-8")
    with pytest.raises(ValueError):
        list(_external(manifest))
