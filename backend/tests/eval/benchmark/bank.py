"""The benchmark question bank — typed loading, validation, golden-set adapter (#420, #430).

``questions.csv`` is the reviewable data file — a wide, Excel-friendly CSV
(UTF-8 with BOM, up to two evidence spans per row, ``|``-separated list cells);
this module is its single loader and the executable spec of its schema. Every
rule the README states about the bank is enforced here — a malformed or
ungrounded-by-construction question fails validation, not review.

Question categories (the benchmark's coverage axes — see README for the
published-evidence base behind them: CRAG's real-user taxonomy, the Salesforce
enterprise deep-search benchmark, and practitioner query-intent reports):

* ``single_hop``  — answer supported by one passage of one document.
* ``multi_hop``   — requires combining passages from ≥ 2 documents.
* ``aggregation`` — reading a table / comparing rows (XLSX or table-heavy PDF).
* ``keyword``     — rare-token lookup (ids, codes, option names) that lexical
  retrieval should win.
* ``distractor``  — a near-miss document exists (named in ``distractor_files``);
  the right answer requires discriminating between confusable sources.
* ``multilingual``— evidence lives in a non-English document.
* ``condition``   — the answer depends on a stated condition ("if I am X…"),
  usually one row/branch of a table or rule (CRAG: simple-with-condition).
* ``set``         — the answer is a list of items (CRAG: set questions).
* ``comparison``  — two named things compared on one attribute (CRAG).
* ``post_processing`` — the passage must be transformed/computed over, not just
  quoted (CRAG: post-processing-heavy).
* ``false_premise``— the question embeds a wrong assumption; the correct answer
  corrects it from the sources (CRAG: false-premise). ``notes`` must state the
  premise being corrected.
* ``procedural``  — "how do I…" workplace intent (enterprise-search evidence).
* ``navigation``  — "which document covers…" artifact-finding intent
  (enterprise-search evidence); evidence quotes self-identifying text.
* ``unanswerable``— nothing in the corpus answers it; the correct behaviour is
  an honest, zero-citation refusal (AC-3). ``absence_probes`` are strings that
  must NOT occur in any extracted corpus text — the machine-checkable side of
  "the corpus really does not contain this".

Grounding contract: every ``evidence.quote`` is a **verbatim substring of the
text the real parsers extract** for that file (whitespace-normalized), and every
``answer_fact`` appears inside at least one of the question's quotes — so the
gold data is retrievable and citable *by construction*, which the corpus-level
checks in :mod:`tests.eval.benchmark.verify` prove.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tests.eval.benchmark.manifest import (
    CORPUS,
    QUESTIONS_PATH,
    CorpusFile,
    entry_by_id,
)
from tests.eval.golden import GoldenDocument, GoldenQuestion

Category = Literal[
    "single_hop",
    "multi_hop",
    "aggregation",
    "keyword",
    "distractor",
    "multilingual",
    "condition",
    "set",
    "comparison",
    "post_processing",
    "false_premise",
    "procedural",
    "navigation",
    "unanswerable",
]
Difficulty = Literal["easy", "medium", "hard"]

_CATEGORIES: frozenset[str] = frozenset(
    {
        "single_hop",
        "multi_hop",
        "aggregation",
        "keyword",
        "distractor",
        "multilingual",
        "condition",
        "set",
        "comparison",
        "post_processing",
        "false_premise",
        "procedural",
        "navigation",
        "unanswerable",
    }
)
_DIFFICULTIES: frozenset[str] = frozenset({"easy", "medium", "hard"})
_QID_PATTERN = re.compile(r"^bm-\d{3}$")

# Normalized-quote length bounds. Lower bound keeps quotes meaningful (a short
# fragment matches spuriously); upper bound keeps a quote retrievable inside a
# single production chunk (INGESTION_CHUNK_SIZE=1200 / overlap=200 — quotes are
# authored ≤ ~200 chars where possible, hard-capped at 300, and aligned to
# sentence boundaries so the boundary-aware chunker rarely splits them).
_QUOTE_MIN_CHARS = 30
_QUOTE_MAX_CHARS = 300

# Minimum per-category counts — the executable floor of the bank's coverage
# shape (RAG-benchmark practice: no category degenerates as the bank evolves).
_MIN_PER_CATEGORY: dict[Category, int] = {
    "single_hop": 20,
    "multi_hop": 5,
    "aggregation": 6,
    "keyword": 4,
    "distractor": 4,
    "multilingual": 2,
    "condition": 2,
    "set": 2,
    "comparison": 2,
    "post_processing": 2,
    "false_premise": 2,
    "procedural": 2,
    "navigation": 2,
    "unanswerable": 10,
}
# Unanswerable share of the whole bank (hallucination-pressure band).
_UNANSWERABLE_SHARE = (0.10, 0.25)


@dataclass(frozen=True, slots=True)
class Evidence:
    """One supporting span: which file, where (human hint), and the exact quote."""

    file_id: str
    locator: str
    quote: str


@dataclass(frozen=True, slots=True)
class BenchmarkQuestion:
    """One question of the bank (see module docstring for the field contract)."""

    qid: str
    question: str
    category: Category
    difficulty: Difficulty
    answerable: bool
    gold_answer: str
    answer_facts: tuple[str, ...]
    source_files: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    distractor_files: tuple[str, ...] = ()
    absence_probes: tuple[str, ...] = ()
    language: str = "en"
    notes: str = ""


def normalize(text: str) -> str:
    """Whitespace-collapse + casefold — the same laxness the eval metrics use.

    Mirrors :func:`tests.eval.metrics._normalize` so "quote appears in document"
    here means exactly what ``passages_overlap`` will accept at scoring time.
    """
    return re.sub(r"\s+", " ", text).strip().casefold()


# The CSV schema: wide, Excel-friendly, one row per question. List cells are
# " | "-separated (validation rejects '|' inside items); up to two evidence
# spans per row (evidence1_*/evidence2_*) — enough for every current category,
# and a structural nudge to keep gold evidence focused. UTF-8 with BOM so
# Excel opens the curly quotes and the German text correctly.
CSV_COLUMNS: tuple[str, ...] = (
    "qid",
    "category",
    "difficulty",
    "answerable",
    "language",
    "question",
    "gold_answer",
    "answer_facts",
    "source_files",
    "evidence1_file",
    "evidence1_locator",
    "evidence1_quote",
    "evidence2_file",
    "evidence2_locator",
    "evidence2_quote",
    "distractor_files",
    "absence_probes",
    "notes",
)
_LIST_SEPARATOR = " | "


def _split_list(cell: str) -> tuple[str, ...]:
    """Parse a ``|``-separated CSV list cell (empty cell -> empty tuple)."""
    return tuple(item.strip() for item in cell.split("|") if item.strip())


def load_questions(path: Path = QUESTIONS_PATH) -> tuple[BenchmarkQuestion, ...]:
    """Parse ``questions.csv`` into typed questions (schema errors raise)."""
    questions: list[BenchmarkQuestion] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or tuple(reader.fieldnames) != CSV_COLUMNS:
            raise ValueError(
                f"questions.csv header mismatch: expected {CSV_COLUMNS}, "
                f"got {tuple(reader.fieldnames or ())}"
            )
        for row_no, raw in enumerate(reader, start=2):  # row 1 is the header
            answerable_cell = raw["answerable"].strip().lower()
            if answerable_cell not in {"yes", "no"}:
                raise ValueError(f"questions.csv row {row_no}: answerable must be yes/no")
            evidence: list[Evidence] = []
            for slot in ("evidence1", "evidence2"):
                if raw[f"{slot}_file"].strip() or raw[f"{slot}_quote"].strip():
                    evidence.append(
                        Evidence(
                            file_id=raw[f"{slot}_file"].strip(),
                            locator=raw[f"{slot}_locator"].strip(),
                            quote=raw[f"{slot}_quote"],
                        )
                    )
            questions.append(
                BenchmarkQuestion(
                    qid=raw["qid"].strip(),
                    question=raw["question"].strip(),
                    category=raw["category"].strip(),  # type: ignore[arg-type]
                    difficulty=raw["difficulty"].strip(),  # type: ignore[arg-type]
                    answerable=answerable_cell == "yes",
                    gold_answer=raw["gold_answer"].strip(),
                    answer_facts=_split_list(raw["answer_facts"]),
                    source_files=_split_list(raw["source_files"]),
                    evidence=tuple(evidence),
                    distractor_files=_split_list(raw["distractor_files"]),
                    absence_probes=_split_list(raw["absence_probes"]),
                    language=raw["language"].strip() or "en",
                    notes=raw["notes"].strip(),
                )
            )
    return tuple(questions)


@dataclass(frozen=True, slots=True)
class BankIssue:
    """One validation failure in the question bank (empty list = healthy)."""

    qid: str
    problem: str


def _validate_one(q: BenchmarkQuestion, ok_ids: set[str], good_ids: set[str]) -> list[BankIssue]:
    issues: list[BankIssue] = []

    def bad(problem: str) -> None:
        issues.append(BankIssue(q.qid, problem))

    if not _QID_PATTERN.match(q.qid):
        bad("qid must match bm-NNN")
    if not q.question.strip().endswith("?"):
        bad("question must end with '?'")
    if q.category not in _CATEGORIES:
        bad(f"unknown category {q.category!r}")
    if q.difficulty not in _DIFFICULTIES:
        bad(f"unknown difficulty {q.difficulty!r}")
    if (q.category == "unanswerable") != (not q.answerable):
        bad("category 'unanswerable' must match answerable=false (and only then)")

    evidence_files = tuple(sorted({ev.file_id for ev in q.evidence}))
    if q.answerable:
        if not q.evidence:
            bad("answerable question needs >= 1 evidence span")
        if not q.gold_answer.strip():
            bad("answerable question needs a gold_answer")
        if not q.answer_facts:
            bad("answerable question needs >= 1 answer_fact")
        if q.absence_probes:
            bad("absence_probes are for unanswerable questions only")
        if tuple(sorted(q.source_files)) != evidence_files:
            bad("source_files must equal the sorted set of evidence file_ids")
        for fact in q.answer_facts:
            if not any(normalize(fact) in normalize(ev.quote) for ev in q.evidence):
                bad(f"answer_fact {fact!r} not found in any evidence quote")
    else:
        if q.evidence or q.source_files or q.answer_facts or q.gold_answer.strip():
            bad("unanswerable question must have no evidence/sources/facts/gold_answer")
        if not q.absence_probes:
            bad("unanswerable question needs >= 1 absence_probe")

    for ev in q.evidence:
        if ev.file_id not in ok_ids:
            bad(f"evidence references unknown/non-ingestable file {ev.file_id!r}")
            continue
        if ev.file_id not in good_ids:
            bad(f"evidence references poor-extraction file {ev.file_id!r}")
        normalized_len = len(normalize(ev.quote))
        if normalized_len < _QUOTE_MIN_CHARS:
            bad(f"quote too short ({normalized_len} < {_QUOTE_MIN_CHARS} chars normalized)")
        if normalized_len > _QUOTE_MAX_CHARS:
            bad(f"quote too long ({normalized_len} > {_QUOTE_MAX_CHARS} chars normalized)")
        if not ev.locator.strip():
            bad("evidence needs a human locator (page/sheet/section hint)")

    if q.category == "multi_hop" and len(evidence_files) < 2:
        bad("multi_hop question must draw evidence from >= 2 documents")
    if q.category == "distractor":
        if not q.distractor_files:
            bad("distractor question must name >= 1 distractor_files")
        for fid in q.distractor_files:
            if fid not in ok_ids:
                bad(f"distractor_files references unknown/non-ingestable file {fid!r}")
            if fid in q.source_files:
                bad(f"distractor file {fid!r} cannot also be a source file")
    if q.category == "multilingual":
        non_english = [fid for fid in evidence_files if entry_by_id(fid).language != "en"]
        if not non_english:
            bad("multilingual question must draw evidence from a non-English document")

    return issues


def bank_issues(
    questions: tuple[BenchmarkQuestion, ...],
    corpus: tuple[CorpusFile, ...] = CORPUS,
) -> list[BankIssue]:
    """Validate the whole bank offline (no downloaded corpus required)."""
    ok_ids = {e.file_id for e in corpus if e.expected_ingest == "ok"}
    good_ids = {e.file_id for e in corpus if e.expected_ingest == "ok" and e.text_quality == "good"}
    issues: list[BankIssue] = []

    seen_qids: set[str] = set()
    seen_questions: set[str] = set()
    for q in questions:
        if q.qid in seen_qids:
            issues.append(BankIssue(q.qid, "duplicate qid"))
        seen_qids.add(q.qid)
        normalized_question = normalize(q.question)
        if normalized_question in seen_questions:
            issues.append(BankIssue(q.qid, "duplicate question text"))
        seen_questions.add(normalized_question)
        issues.extend(_validate_one(q, ok_ids, good_ids))

    counts = Counter(q.category for q in questions)
    for category, minimum in _MIN_PER_CATEGORY.items():
        if counts.get(category, 0) < minimum:
            issues.append(
                BankIssue(
                    "<bank>",
                    f"category {category!r} has {counts.get(category, 0)} questions "
                    f"(minimum {minimum})",
                )
            )
    if questions:
        share = counts.get("unanswerable", 0) / len(questions)
        low, high = _UNANSWERABLE_SHARE
        if not (low <= share <= high):
            issues.append(
                BankIssue(
                    "<bank>",
                    f"unanswerable share {share:.0%} outside the {low:.0%}–{high:.0%} band",
                )
            )
    return issues


# --- Golden-set adapter ---------------------------------------------------------
# The existing eval harness (tests/eval/harness.py) consumes GoldenDocument /
# GoldenQuestion. The benchmark exposes itself in that shape so the offline and
# live eval can run over real files without new machinery. Only single-evidence
# answerable questions map cleanly (GoldenQuestion carries exactly one
# supporting passage); multi-evidence categories are for the fuller benchmark
# runner and manual validation.


def _adaptable(q: BenchmarkQuestion, allowed_files: set[str]) -> bool:
    if not q.answerable:
        return True  # honest-refusal case adapts as-is
    return len(q.evidence) == 1 and q.evidence[0].file_id in allowed_files


def golden_documents(
    *,
    subset: Literal["smoke", "full"] = "smoke",
    corpus: tuple[CorpusFile, ...] = CORPUS,
) -> tuple[GoldenDocument, ...]:
    """The benchmark corpus as harness-seedable documents (extracted text).

    Requires the corpus to be downloaded + extractable; reads through the
    extraction cache. ``smoke`` is the one-file-per-format subset sized for a
    cheap live run; ``full`` is every ingestable good-extraction file.
    """
    from tests.eval.benchmark.extract import cached_text

    chosen = [
        e
        for e in corpus
        if e.expected_ingest == "ok" and e.text_quality == "good" and (subset == "full" or e.smoke)
    ]
    return tuple(
        GoldenDocument(doc_id=e.file_id, filename=e.filename, text=cached_text(e.file_id))
        for e in chosen
    )


def golden_questions(
    *,
    subset: Literal["smoke", "full"] = "smoke",
    questions: tuple[BenchmarkQuestion, ...] | None = None,
    corpus: tuple[CorpusFile, ...] = CORPUS,
) -> tuple[GoldenQuestion, ...]:
    """The single-evidence slice of the bank as harness-scorable questions."""
    if questions is None:
        questions = load_questions()
    allowed = {
        e.file_id
        for e in corpus
        if e.expected_ingest == "ok" and e.text_quality == "good" and (subset == "full" or e.smoke)
    }
    adapted: list[GoldenQuestion] = []
    for q in questions:
        if not _adaptable(q, allowed):
            continue
        if q.answerable:
            adapted.append(
                GoldenQuestion(
                    qid=q.qid,
                    question=q.question,
                    supporting_document=q.evidence[0].file_id,
                    supporting_passage=q.evidence[0].quote,
                    answer_facts=q.answer_facts,
                )
            )
        else:
            adapted.append(
                GoldenQuestion(
                    qid=q.qid,
                    question=q.question,
                    supporting_document=None,
                    supporting_passage="",
                    answer_facts=(),
                )
            )
    return tuple(adapted)


__all__ = [
    "BankIssue",
    "BenchmarkQuestion",
    "Category",
    "Difficulty",
    "Evidence",
    "bank_issues",
    "golden_documents",
    "golden_questions",
    "load_questions",
    "normalize",
]
