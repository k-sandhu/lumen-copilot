"""The golden Q/A-with-source set — the eval corpus, pinned in-repo (#29).

A small, hand-authored corpus + question set is the fixture both the offline and
the live eval run against (ADR-0003 §9: "golden Q/A-with-source set"). Keeping it
in code (not a fixture file) makes the expected answer, the supporting passage,
and the document it belongs to reviewable in one place and diff-able when the
corpus changes.

Shapes:

* :class:`GoldenDocument` — one seeded document: a filename + its full text. The
  offline ingestion path (real chunker) and the live ingestion path both turn
  this into chunks; a :class:`GoldenQuestion` names the substring of the document
  text that *should* support the answer, so a retrieved/cited passage can be
  scored against it without pinning brittle chunk boundaries.
* :class:`GoldenQuestion` — a question, the id of the document that answers it
  (``None`` for the unanswerable "couldn't find it" case), the supporting
  passage substring, and a list of key facts the answer should contain. A
  question with ``supporting_document is None`` is the **honest-refusal** case
  (AC-3): the correct behaviour is *zero* citations and a "couldn't find it"
  answer.

The corpus is deliberately tiny and self-contained (no external files, no PII)
so it is deterministic and cheap to run live.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class GoldenDocument:
    """One seeded document in the golden corpus.

    ``doc_id`` is a stable in-fixture handle (not a DB id) used to tie a question
    to the document that answers it; ``filename`` and ``text`` are what gets
    uploaded/ingested. ``text`` is plain text so the real chunker (#21) produces
    deterministic chunks/offsets.
    """

    doc_id: str
    filename: str
    text: str


@dataclass(frozen=True, slots=True)
class GoldenQuestion:
    """One question with its expected supporting source + answer facts.

    ``supporting_document`` is the ``doc_id`` of the document that answers the
    question, or ``None`` for the unanswerable case (the honest-refusal AC-3).
    ``supporting_passage`` is a substring of that document's text that should be
    retrieved + cited; scoring matches a retrieved/cited passage against this
    substring rather than exact chunk spans (robust to chunk-boundary changes).
    ``answer_facts`` are key tokens an answer should contain (a coarse content
    check for the live run; the offline run drives a scripted answer).
    """

    qid: str
    question: str
    supporting_document: str | None
    supporting_passage: str
    answer_facts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_answerable(self) -> bool:
        """True iff a permitted source is expected to support an answer."""
        return self.supporting_document is not None


# --- The corpus -------------------------------------------------------------
# Three short documents covering distinct topics so retrieval has to discriminate
# (a question must pull the *right* document, not just any). Each document's text
# is plain prose; the supporting passages below are verbatim substrings of it.

GOLDEN_DOCUMENTS: tuple[GoldenDocument, ...] = (
    GoldenDocument(
        doc_id="tax-guide",
        filename="tax-guide-2024.txt",
        text=(
            "US Federal Income Tax Guide for 2024.\n\n"
            "The standard deduction for a single filer in 2024 is $14,600. "
            "For married couples filing jointly, the standard deduction is $29,200. "
            "Heads of household may claim a standard deduction of $21,900.\n\n"
            "The deadline to file a federal income tax return for the 2024 tax year "
            "is April 15, 2025. An automatic six-month extension to file may be "
            "requested, but it does not extend the deadline to pay any tax owed.\n"
        ),
    ),
    GoldenDocument(
        doc_id="pto-policy",
        filename="acme-pto-policy.txt",
        text=(
            "Acme Corporation Paid Time Off (PTO) Policy.\n\n"
            "Full-time employees accrue 18 days of paid time off per calendar year. "
            "Accrual begins on the first day of employment and is granted monthly. "
            "Up to 5 unused PTO days may be carried over into the following year; "
            "any balance above 5 days is forfeited on December 31.\n\n"
            "Requests for PTO must be submitted at least two weeks in advance for "
            "any absence longer than three consecutive days.\n"
        ),
    ),
    GoldenDocument(
        doc_id="security-runbook",
        filename="incident-runbook.txt",
        text=(
            "Security Incident Response Runbook.\n\n"
            "On detecting a suspected data breach, the on-call engineer must declare "
            "an incident within 15 minutes and page the security lead. All affected "
            "credentials are rotated immediately. A written postmortem is required "
            "within five business days of incident resolution.\n\n"
            "Customer notification, when legally required, is sent within 72 hours "
            "of confirming that customer data was exposed.\n"
        ),
    ),
)


GOLDEN_QUESTIONS: tuple[GoldenQuestion, ...] = (
    GoldenQuestion(
        qid="q-standard-deduction",
        question="What is the 2024 standard deduction for a single filer?",
        supporting_document="tax-guide",
        supporting_passage="The standard deduction for a single filer in 2024 is $14,600.",
        answer_facts=("$14,600",),
    ),
    GoldenQuestion(
        qid="q-tax-deadline",
        question="When is the 2024 federal tax return due?",
        supporting_document="tax-guide",
        supporting_passage=(
            "The deadline to file a federal income tax return for the 2024 tax year "
            "is April 15, 2025."
        ),
        answer_facts=("April 15", "2025"),
    ),
    GoldenQuestion(
        qid="q-pto-accrual",
        question="How many PTO days do full-time employees accrue per year?",
        supporting_document="pto-policy",
        supporting_passage="Full-time employees accrue 18 days of paid time off per calendar year.",
        answer_facts=("18",),
    ),
    GoldenQuestion(
        qid="q-pto-carryover",
        question="How many unused PTO days can be carried over?",
        supporting_document="pto-policy",
        supporting_passage="Up to 5 unused PTO days may be carried over into the following year;",
        answer_facts=("5",),
    ),
    GoldenQuestion(
        qid="q-breach-notification",
        question="How quickly must customers be notified after a confirmed data breach?",
        supporting_document="security-runbook",
        supporting_passage=(
            "Customer notification, when legally required, is sent within 72 hours "
            "of confirming that customer data was exposed."
        ),
        answer_facts=("72 hours",),
    ),
    # The honest-refusal case (AC-3 / INV-3): nothing in the corpus answers this,
    # so the correct behaviour is a zero-citation "couldn't find it" answer.
    GoldenQuestion(
        qid="q-unanswerable",
        question="What is the company's stock ticker symbol?",
        supporting_document=None,
        supporting_passage="",
        answer_facts=(),
    ),
)


def document_by_id(doc_id: str) -> GoldenDocument:
    """Return the golden document with ``doc_id`` (raises if unknown)."""
    for doc in GOLDEN_DOCUMENTS:
        if doc.doc_id == doc_id:
            return doc
    raise KeyError(f"unknown golden document id: {doc_id}")


__all__ = [
    "GOLDEN_DOCUMENTS",
    "GOLDEN_QUESTIONS",
    "GoldenDocument",
    "GoldenQuestion",
    "document_by_id",
]
