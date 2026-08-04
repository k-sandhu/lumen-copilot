"""The benchmark dataset's executable guarantees (#420) — prose ↔ mechanism.

Layer 1 (always runs, offline, no corpus): the manifest is structurally sound,
every entry carries a checksum pin, the question bank passes every schema and
coverage rule, and the golden-set adapter produces harness-consumable shapes.

Layer 2 (runs iff the corpus has been downloaded — each test skips with the
download command otherwise): downloaded bytes match the pins, the REAL parsers
extract usable text from every good file (and near-nothing from the pinned
scanned-PDF edge case), every evidence quote is found in the extracted text of
its source file, absence probes hit nothing, and the deliberately-disallowed
formats fail closed with the typed error (spec 0004 AC-6 flavour of INV-8).

No test here opens a network socket; layer 2 reads local files only, so the
default offline ``pytest`` run stays socket-free (#94) whether or not the
corpus is present.
"""

from __future__ import annotations

import pytest

from app.ingestion.parsers import UnsupportedMimeTypeError, parse_document
from tests.eval.benchmark.bank import (
    bank_issues,
    golden_questions,
    load_questions,
    normalize,
)
from tests.eval.benchmark.manifest import (
    CORPUS,
    corpus_dir,
    load_checksums,
    manifest_issues,
)
from tests.eval.benchmark.verify import (
    GOOD_EXTRACTION_MIN_CHARS,
    POOR_EXTRACTION_MAX_CHARS,
    grounding_findings,
    probe_findings,
)

# --- Layer 1: offline ---------------------------------------------------------


def test_manifest_is_structurally_sound() -> None:
    """Unique ids/names, https URLs, allowlist consistency, smoke coverage."""
    assert manifest_issues() == []


def test_every_entry_has_a_checksum_pin() -> None:
    """Reproducibility contract: no entry without a pinned sha256 + size."""
    pins = load_checksums()
    unpinned = [e.file_id for e in CORPUS if e.file_id not in pins]
    assert unpinned == [], f"entries without pins (run download --pin): {unpinned}"
    stale = [fid for fid in pins if not any(e.file_id == fid for e in CORPUS)]
    assert stale == [], f"pins for entries no longer in the manifest: {stale}"
    assert all(len(pin.sha256) == 64 for pin in pins.values())


def test_question_bank_passes_schema_and_coverage() -> None:
    """Every bank rule (shapes, referential integrity, category floors) holds."""
    issues = bank_issues(load_questions())
    assert issues == [], "\n".join(f"{i.qid}: {i.problem}" for i in issues)


def test_answer_facts_are_contained_in_evidence() -> None:
    """The facts an answer is scored on are retrievable by construction."""
    for q in load_questions():
        for fact in q.answer_facts:
            assert any(
                normalize(fact) in normalize(ev.quote) for ev in q.evidence
            ), f"{q.qid}: fact {fact!r} missing from its evidence quotes"


def test_golden_adapter_yields_harness_consumable_questions() -> None:
    """The single-evidence slice adapts into the existing eval harness shapes."""
    adapted = golden_questions(subset="full")
    answerable = [g for g in adapted if g.is_answerable]
    refusals = [g for g in adapted if not g.is_answerable]
    assert len(answerable) >= 20, "adapter should carry a meaningful answerable slice"
    assert len(refusals) >= 10, "the honest-refusal cases must survive adaptation"
    for g in answerable:
        assert g.supporting_document is not None
        assert g.supporting_passage, f"{g.qid}: adapted question lost its passage"
        assert g.answer_facts, f"{g.qid}: adapted question lost its answer facts"
    smoke = golden_questions(subset="smoke")
    smoke_answerable = [g for g in smoke if g.is_answerable]
    assert smoke_answerable, "smoke subset must keep at least one answerable question"


# --- Layer 2: needs the downloaded corpus --------------------------------------


def _corpus_complete() -> bool:
    directory = corpus_dir()
    return all((directory / entry.filename).exists() for entry in CORPUS)


needs_corpus = pytest.mark.skipif(
    not _corpus_complete(),
    reason=(
        "benchmark corpus not downloaded — run "
        "`uv run --extra dev python -m tests.eval.benchmark.download` first"
    ),
)


@pytest.fixture(scope="module")
def extracted() -> dict[str, str]:
    """Extracted (and disk-cached) text of every ingestable corpus file."""
    from tests.eval.benchmark.extract import cached_text

    return {
        entry.file_id: cached_text(entry.file_id)
        for entry in CORPUS
        if entry.expected_ingest == "ok"
    }


@needs_corpus
def test_corpus_bytes_match_pinned_sizes() -> None:
    """Cheap integrity gate (full sha256 re-hash lives in the verify CLI)."""
    pins = load_checksums()
    directory = corpus_dir()
    for entry in CORPUS:
        observed = (directory / entry.filename).stat().st_size
        assert observed == pins[entry.file_id].size_bytes, (
            f"{entry.file_id}: {observed:,} B on disk != pinned "
            f"{pins[entry.file_id].size_bytes:,} B"
        )


@needs_corpus
def test_parsers_extract_usable_text_from_every_good_file(extracted: dict[str, str]) -> None:
    """The real parsers get non-trivial text out of every good-quality file."""
    for entry in CORPUS:
        if entry.expected_ingest != "ok" or entry.text_quality != "good":
            continue
        chars = len(extracted[entry.file_id].strip())
        assert chars >= GOOD_EXTRACTION_MIN_CHARS, f"{entry.file_id}: only {chars} chars extracted"


@needs_corpus
def test_poor_extraction_edge_case_stays_pinned(extracted: dict[str, str]) -> None:
    """The pinned extraction limitation stays pinned: 'poor' files yield ~no text.

    Today's case: a DOCX whose content lives in Word tables, which the
    paragraphs-only parser skips. If this ever FAILS in the other direction
    (lots of text extracted), the parser gained a capability — reclassify the
    entry's ``text_quality`` and add real questions for the file instead of
    deleting the assertion.
    """
    poor = [e for e in CORPUS if e.text_quality == "poor"]
    assert poor, "manifest should keep a poor-extraction edge case"
    for entry in poor:
        chars = len(extracted[entry.file_id].strip())
        assert (
            chars <= POOR_EXTRACTION_MAX_CHARS
        ), f"{entry.file_id}: poor-quality file unexpectedly extracted {chars} chars"


@needs_corpus
def test_every_evidence_quote_grounds_in_extracted_text(extracted: dict[str, str]) -> None:
    """The core groundedness guarantee: quotes are substrings of parser output."""
    normalized = {fid: normalize(text) for fid, text in extracted.items()}
    findings = grounding_findings(load_questions(), normalized)
    assert findings == [], "\n".join(f"{f.subject}: {f.problem}" for f in findings)


@needs_corpus
def test_absence_probes_hit_nothing(extracted: dict[str, str]) -> None:
    """Unanswerable questions are machine-checked to really be unanswerable."""
    normalized = {fid: normalize(text) for fid, text in extracted.items()}
    findings = probe_findings(load_questions(), normalized)
    assert findings == [], "\n".join(f"{f.subject}: {f.problem}" for f in findings)


@needs_corpus
def test_disallowed_formats_fail_closed() -> None:
    """The negative entries are rejected with the typed allowlist error (AC-6)."""
    directory = corpus_dir()
    negatives = [e for e in CORPUS if e.expected_ingest == "rejected_type"]
    assert negatives, "manifest should keep disallowed-format negatives"
    for entry in negatives:
        data = (directory / entry.filename).read_bytes()
        with pytest.raises(UnsupportedMimeTypeError):
            parse_document(data, mime_type=entry.mime_type)
