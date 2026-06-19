"""Unit tests for the eval metrics — the harness's own correctness (#29).

These prove the metric functions score the way the spec requires *before* they
are pointed at a real answer (ADR-0003 §9: the harness is itself testable). They
include the headline quality negatives the issue calls out:

* an answer with an **unsupported claim** (facts, zero citations) FAILS
  groundedness;
* a citation to a **non-permitted** passage (a different owner) FAILS
  citation-correctness (INV-3);
* a citation to the **wrong** source FAILS citation-correctness;
* the honest **refusal** with zero citations PASSES groundedness, and a refusal
  that nonetheless carries citations FAILS.

No DB, no model — pure functions over value objects.
"""

from __future__ import annotations

import uuid

from tests.eval.metrics import (
    ObservedAnswer,
    ObservedPassage,
    citation_correctness,
    groundedness,
    is_refusal,
    passages_overlap,
    retrieval_recall,
)

_ASKER = uuid.uuid4()
_OTHER_OWNER = uuid.uuid4()
_DOC = uuid.uuid4()

_SUPPORTING = "The standard deduction for a single filer in 2024 is $14,600."


def _passage(
    text: str, *, owner: uuid.UUID = _ASKER, name: str = "tax-guide-2024.txt"
) -> ObservedPassage:
    return ObservedPassage(document_id=_DOC, document_name=name, text=text, owner_id=owner)


# --- passages_overlap -------------------------------------------------------


def test_overlap_matches_substring_either_direction() -> None:
    big = "Intro. " + _SUPPORTING + " More text after."
    assert passages_overlap(big, _SUPPORTING) is True
    assert passages_overlap(_SUPPORTING, big) is True


def test_overlap_is_whitespace_and_case_insensitive() -> None:
    spaced = "  the   STANDARD deduction for a single filer in 2024 is $14,600.  "
    assert passages_overlap(spaced, _SUPPORTING) is True


def test_overlap_blank_never_matches() -> None:
    assert passages_overlap("", _SUPPORTING) is False
    assert passages_overlap(_SUPPORTING, "") is False
    assert passages_overlap("totally unrelated text", _SUPPORTING) is False


# --- groundedness -----------------------------------------------------------


def test_grounded_answer_with_citation_passes() -> None:
    observed = ObservedAnswer(
        retrieved=(_passage(_SUPPORTING),),
        citations=(_passage(_SUPPORTING),),
        answer_text="The 2024 single-filer standard deduction is $14,600.",
        asked_by=_ASKER,
    )
    assert groundedness(observed) is True


def test_unsupported_claim_fails_groundedness() -> None:
    # The headline negative: a fact-asserting answer with NO citation is ungrounded.
    observed = ObservedAnswer(
        retrieved=(),
        citations=(),
        answer_text="The 2024 single-filer standard deduction is $14,600.",
        asked_by=_ASKER,
    )
    assert groundedness(observed) is False


def test_honest_refusal_with_no_citation_is_grounded() -> None:
    observed = ObservedAnswer(
        retrieved=(),
        citations=(),
        answer_text="I couldn't find anything in your sources that answers that.",
        asked_by=_ASKER,
    )
    assert is_refusal(observed.answer_text) is True
    assert groundedness(observed) is True


def test_refusal_with_citations_fails_groundedness() -> None:
    # A "couldn't find it" that nonetheless attaches citations is incoherent → fail.
    observed = ObservedAnswer(
        retrieved=(_passage(_SUPPORTING),),
        citations=(_passage(_SUPPORTING),),
        answer_text="I couldn't find anything relevant.",
        asked_by=_ASKER,
    )
    assert groundedness(observed) is False


# --- citation_correctness (incl. INV-3 negatives) ---------------------------


def test_correct_citation_to_expected_permitted_source_passes() -> None:
    observed = ObservedAnswer(
        retrieved=(_passage(_SUPPORTING),),
        citations=(_passage(_SUPPORTING),),
        answer_text="$14,600.",
        asked_by=_ASKER,
    )
    assert (
        citation_correctness(
            observed,
            expected_passage=_SUPPORTING,
            expected_document_name="tax-guide-2024.txt",
        )
        is True
    )


def test_citation_to_non_permitted_passage_fails_inv3() -> None:
    # INV-3 negative: a citation whose passage is owned by ANOTHER user fails,
    # even though its text matches the expected supporting passage.
    observed = ObservedAnswer(
        retrieved=(_passage(_SUPPORTING),),
        citations=(_passage(_SUPPORTING, owner=_OTHER_OWNER),),
        answer_text="$14,600.",
        asked_by=_ASKER,
    )
    assert (
        citation_correctness(
            observed,
            expected_passage=_SUPPORTING,
            expected_document_name="tax-guide-2024.txt",
        )
        is False
    )


def test_citation_to_wrong_document_fails() -> None:
    # Right text overlap but wrong source document → not the expected evidence.
    observed = ObservedAnswer(
        retrieved=(_passage(_SUPPORTING),),
        citations=(_passage(_SUPPORTING, name="some-other-doc.txt"),),
        answer_text="$14,600.",
        asked_by=_ASKER,
    )
    assert (
        citation_correctness(
            observed,
            expected_passage=_SUPPORTING,
            expected_document_name="tax-guide-2024.txt",
        )
        is False
    )


def test_no_citation_fails_correctness_for_answerable() -> None:
    observed = ObservedAnswer(
        retrieved=(_passage(_SUPPORTING),),
        citations=(),
        answer_text="$14,600.",
        asked_by=_ASKER,
    )
    assert (
        citation_correctness(
            observed,
            expected_passage=_SUPPORTING,
            expected_document_name="tax-guide-2024.txt",
        )
        is False
    )


# --- retrieval_recall -------------------------------------------------------


def test_recall_true_when_supporting_passage_retrieved() -> None:
    observed = ObservedAnswer(
        retrieved=(_passage("noise"), _passage(_SUPPORTING)),
        citations=(),
        answer_text="x",
        asked_by=_ASKER,
    )
    assert retrieval_recall(observed, expected_passage=_SUPPORTING) is True


def test_recall_false_when_supporting_passage_absent() -> None:
    observed = ObservedAnswer(
        retrieved=(_passage("unrelated passage about cats"),),
        citations=(),
        answer_text="x",
        asked_by=_ASKER,
    )
    assert retrieval_recall(observed, expected_passage=_SUPPORTING) is False
