"""Verify the benchmark dataset end-to-end — offline shape + corpus grounding (#420).

Two layers of checks, mirrored 1:1 by ``tests/eval/test_benchmark_dataset.py``:

* **Offline** (no downloads needed; always run): the manifest is structurally
  sound, every entry has a checksum pin, and the question bank passes every
  schema/coverage rule in :mod:`tests.eval.benchmark.bank`.
* **Corpus** (needs a downloaded corpus): downloaded bytes match their pins;
  the real parsers extract non-trivial text from every ``text_quality="good"``
  file (and near-nothing from the pinned ``poor`` scan); **every evidence quote
  is found in the extracted text of its file**; every absence probe of every
  unanswerable question appears in NO extracted text; and the deliberate
  negative formats are rejected by the parser fail-closed path.

Usage (from ``backend/``):

    uv run --extra dev python -m tests.eval.benchmark.verify            # both layers
    uv run --extra dev python -m tests.eval.benchmark.verify --offline  # schema only
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from app.ingestion.parsers import DocumentParseError, UnsupportedMimeTypeError, parse_document
from tests.eval.benchmark.bank import (
    BenchmarkQuestion,
    bank_issues,
    load_questions,
    normalize,
)
from tests.eval.benchmark.manifest import (
    CORPUS,
    Checksum,
    corpus_dir,
    load_checksums,
    manifest_issues,
)

# A "good" extraction must yield at least this much text; a pinned "poor" edge
# case (e.g. a DOCX whose content sits in Word tables the paragraphs-only parser
# skips) must stay under the max — if it ever grows past it, the parser gained a
# capability and the manifest entry should be reclassified.
GOOD_EXTRACTION_MIN_CHARS = 1_000
POOR_EXTRACTION_MAX_CHARS = 500


@dataclass(frozen=True, slots=True)
class Finding:
    """One verification failure (empty list = the dataset is healthy)."""

    scope: str  # "manifest" | "pins" | "bank" | "corpus" | "grounding" | "probes" | "negative"
    subject: str  # file_id or qid
    problem: str


def offline_findings() -> list[Finding]:
    """Layer 1: manifest structure, pin completeness, bank schema/coverage."""
    findings: list[Finding] = []
    for issue in manifest_issues():
        findings.append(Finding("manifest", issue.file_id, issue.problem))

    pins = load_checksums()
    for entry in CORPUS:
        pin = pins.get(entry.file_id)
        if pin is None:
            findings.append(Finding("pins", entry.file_id, "no checksum pin (run download --pin)"))
        elif len(pin.sha256) != 64:
            findings.append(Finding("pins", entry.file_id, "malformed sha256 pin"))
    for pinned_id in pins:
        if not any(e.file_id == pinned_id for e in CORPUS):
            findings.append(Finding("pins", pinned_id, "pin for unknown file_id (stale entry)"))

    try:
        questions = load_questions()
    except (ValueError, FileNotFoundError) as exc:
        findings.append(Finding("bank", "<bank>", f"questions.csv unreadable: {exc}"))
        return findings
    for bank_issue in bank_issues(questions):
        findings.append(Finding("bank", bank_issue.qid, bank_issue.problem))
    return findings


def _extracted_texts() -> tuple[dict[str, str], list[Finding]]:
    """Extract every ingestable file (via the real parsers), collecting failures."""
    from tests.eval.benchmark.extract import extract_text

    findings: list[Finding] = []
    texts: dict[str, str] = {}
    for entry in CORPUS:
        if entry.expected_ingest != "ok":
            continue
        try:
            texts[entry.file_id] = extract_text(entry)
        except FileNotFoundError:
            findings.append(Finding("corpus", entry.file_id, "not downloaded"))
        except DocumentParseError as exc:
            findings.append(Finding("corpus", entry.file_id, f"parser failed: {exc}"))
    return texts, findings


def corpus_findings(*, check_hashes: bool = True) -> list[Finding]:
    """Layer 2: bytes match pins; extraction, grounding, probes, negatives."""
    findings: list[Finding] = []
    directory = corpus_dir()
    pins = load_checksums()

    # --- bytes match their pins ------------------------------------------------
    for entry in CORPUS:
        path = directory / entry.filename
        if not path.exists():
            findings.append(Finding("corpus", entry.file_id, "not downloaded"))
            continue
        pin = pins.get(entry.file_id)
        if pin is None:
            continue  # already reported offline
        if path.stat().st_size != pin.size_bytes:
            findings.append(
                Finding(
                    "corpus",
                    entry.file_id,
                    f"size {path.stat().st_size:,} B != pinned {pin.size_bytes:,} B",
                )
            )
        elif check_hashes:
            from tests.eval.benchmark.download import _sha256_of

            observed: Checksum = _sha256_of(path)
            if observed.sha256 != pin.sha256:
                findings.append(Finding("corpus", entry.file_id, "sha256 mismatch vs pin"))
    if any(f.problem == "not downloaded" for f in findings):
        return findings  # grounding checks are meaningless on a partial corpus

    # --- extraction quality ------------------------------------------------------
    texts, extraction_findings = _extracted_texts()
    findings.extend(extraction_findings)
    for entry in CORPUS:
        if entry.expected_ingest != "ok" or entry.file_id not in texts:
            continue
        chars = len(texts[entry.file_id].strip())
        if entry.text_quality == "good" and chars < GOOD_EXTRACTION_MIN_CHARS:
            findings.append(
                Finding(
                    "corpus",
                    entry.file_id,
                    f"good-quality file extracted only {chars} chars "
                    f"(< {GOOD_EXTRACTION_MIN_CHARS})",
                )
            )
        if entry.text_quality == "poor" and chars > POOR_EXTRACTION_MAX_CHARS:
            findings.append(
                Finding(
                    "corpus",
                    entry.file_id,
                    f"poor-quality file extracted {chars} chars "
                    f"(> {POOR_EXTRACTION_MAX_CHARS}) — update its text_quality",
                )
            )

    # --- evidence grounding + absence probes -------------------------------------
    normalized = {fid: normalize(text) for fid, text in texts.items()}
    questions = load_questions()
    findings.extend(grounding_findings(questions, normalized))
    findings.extend(probe_findings(questions, normalized))

    # --- negative formats fail closed ---------------------------------------------
    for entry in CORPUS:
        if entry.expected_ingest != "rejected_type":
            continue
        data = (directory / entry.filename).read_bytes()
        try:
            parse_document(data, mime_type=entry.mime_type)
        except UnsupportedMimeTypeError:
            continue  # the expected fail-closed outcome
        except DocumentParseError as exc:
            findings.append(
                Finding(
                    "negative",
                    entry.file_id,
                    f"rejected for the wrong reason ({type(exc).__name__}) — "
                    "expected UnsupportedMimeTypeError",
                )
            )
        else:
            findings.append(
                Finding("negative", entry.file_id, "parser ACCEPTED a disallowed format")
            )
    return findings


def grounding_findings(
    questions: tuple[BenchmarkQuestion, ...],
    normalized_texts: dict[str, str],
) -> list[Finding]:
    """Every evidence quote must appear in the extracted text of its file."""
    findings: list[Finding] = []
    for q in questions:
        for ev in q.evidence:
            doc = normalized_texts.get(ev.file_id)
            if doc is None:
                findings.append(
                    Finding("grounding", q.qid, f"evidence file {ev.file_id} has no extraction")
                )
                continue
            if normalize(ev.quote) not in doc:
                findings.append(
                    Finding(
                        "grounding",
                        q.qid,
                        f"quote not found in extracted text of {ev.file_id}: "
                        f"{ev.quote[:80]!r}…",
                    )
                )
    return findings


def probe_findings(
    questions: tuple[BenchmarkQuestion, ...],
    normalized_texts: dict[str, str],
) -> list[Finding]:
    """No absence probe of an unanswerable question may appear in ANY corpus text."""
    findings: list[Finding] = []
    for q in questions:
        for probe in q.absence_probes:
            hits = [fid for fid, doc in normalized_texts.items() if normalize(probe) in doc]
            if hits:
                findings.append(
                    Finding(
                        "probes",
                        q.qid,
                        f"absence probe {probe!r} occurs in {', '.join(sorted(hits))} — "
                        "the question may actually be answerable",
                    )
                )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the benchmark dataset.")
    parser.add_argument("--offline", action="store_true", help="schema/pin checks only")
    parser.add_argument(
        "--skip-hashes", action="store_true", help="trust sizes; skip sha256 re-hash"
    )
    args = parser.parse_args(argv)

    findings = offline_findings()
    offline_count = len(findings)
    print(f"offline checks: {'OK' if offline_count == 0 else f'{offline_count} finding(s)'}")
    if not args.offline:
        corpus_results = corpus_findings(check_hashes=not args.skip_hashes)
        findings.extend(corpus_results)
        print(
            f"corpus checks:  "
            f"{'OK' if not corpus_results else f'{len(corpus_results)} finding(s)'}"
        )

    for finding in findings:
        print(f"[{finding.scope:>9}] {finding.subject}: {finding.problem}", file=sys.stderr)
    if findings:
        print(f"\nFAIL — {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("\nPASS — dataset is structurally sound and fully grounded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
