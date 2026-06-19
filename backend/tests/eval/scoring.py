"""Score the golden set into an aggregate + the pass/fail thresholds (#29).

Bridges the golden items (:mod:`tests.eval.golden`) and the pure metrics
(:mod:`tests.eval.metrics`): given a question and the answer observed for it,
produce a per-item :class:`~tests.eval.metrics.ItemScore`; given all items,
produce the :class:`~tests.eval.metrics.AggregateScore`; and assert the aggregate
clears the thresholds.

The **thresholds** are the quality bar the eval enforces (ADR-0003 §9: quality is
*measured*, not asserted). They are pinned here so a regression that drops
groundedness or citation-correctness fails the run. On the small, deterministic
golden corpus the offline run is engineered to score 1.0 across the board (the
faked answer is grounded + cites the right permitted passage), so the thresholds
are set to demand exactly that; the live run is allowed a small margin (a real
model may phrase one answer as a refusal) while still demanding no ungrounded or
mis-cited answers in aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.eval.golden import GoldenQuestion
from tests.eval.metrics import (
    AggregateScore,
    ItemScore,
    ObservedAnswer,
    citation_correctness,
    groundedness,
    is_refusal,
    retrieval_recall,
)


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The minimum aggregate metrics an eval run must clear.

    A run is a pass iff every aggregate metric is ``>=`` its threshold here. The
    defaults are the **offline** bar (deterministic → demand perfection);
    :meth:`live` relaxes them to a realistic-model bar that still forbids any
    ungrounded or mis-cited answer in aggregate.
    """

    groundedness: float = 1.0
    citation_correctness: float = 1.0
    retrieval_recall: float = 1.0

    @classmethod
    def live(cls) -> Thresholds:
        """Slightly relaxed thresholds for the real-model / real-stack run.

        A live model may, on a given day, phrase a borderline answer as a refusal
        or miss one retrieval; the bar stays high (no systemic ungrounding) but
        allows a single soft miss on the tiny corpus.
        """
        return cls(groundedness=0.8, citation_correctness=0.8, retrieval_recall=0.8)


def score_item(question: GoldenQuestion, observed: ObservedAnswer) -> ItemScore:
    """Score one golden question against the answer observed for it.

    For an **answerable** question all three metrics apply. For the
    **unanswerable** (honest-refusal) question, the correct outcome is a grounded
    refusal with no citations; we score it as:

    * grounded — via the metric (a zero-citation refusal is grounded);
    * citation-correct — ``True`` iff it carried no citations (it correctly cited
      nothing) **and** read as a refusal; a confident fabricated answer here is
      both ungrounded and not citation-correct;
    * retrieved — not applicable (recall is over answerable items only), recorded
      as ``True`` so it never drags the (separately answerable-only) recall.
    """
    grounded = groundedness(observed)
    if question.is_answerable:
        assert question.supporting_document is not None  # narrows for the type checker
        correct = citation_correctness(
            observed,
            expected_passage=question.supporting_passage,
            expected_document_name=_expected_document_name(question),
        )
        retrieved = retrieval_recall(observed, expected_passage=question.supporting_passage)
        return ItemScore(
            qid=question.qid,
            answerable=True,
            grounded=grounded,
            citation_correct=correct,
            retrieved=retrieved,
        )
    # Unanswerable: a correct refusal cites nothing and reads as a refusal.
    correct_refusal = len(observed.citations) == 0 and is_refusal(observed.answer_text)
    return ItemScore(
        qid=question.qid,
        answerable=False,
        grounded=grounded,
        citation_correct=correct_refusal,
        retrieved=True,
    )


def aggregate(scores: list[ItemScore]) -> AggregateScore:
    """Roll per-item scores into the aggregate the thresholds compare against."""
    return AggregateScore(items=tuple(scores))


def assert_meets(score: AggregateScore, thresholds: Thresholds) -> None:
    """Raise ``AssertionError`` with a readable diagnostic if any metric is short.

    The single gate the eval tests call: a regression that drops a metric below
    its threshold fails here with the offending number, the failing item ids, and
    the full aggregate summary so the failure is actionable, not opaque.
    """
    failures: list[str] = []
    if score.groundedness < thresholds.groundedness:
        ungrounded = [s.qid for s in score.items if not s.grounded]
        failures.append(
            f"groundedness {score.groundedness:.2f} < {thresholds.groundedness:.2f} "
            f"(ungrounded: {ungrounded})"
        )
    if score.citation_correctness < thresholds.citation_correctness:
        miscited = [s.qid for s in score.items if not s.citation_correct]
        failures.append(
            f"citation_correctness {score.citation_correctness:.2f} < "
            f"{thresholds.citation_correctness:.2f} (mis-cited: {miscited})"
        )
    if score.retrieval_recall < thresholds.retrieval_recall:
        missed = [s.qid for s in score.items if s.answerable and not s.retrieved]
        failures.append(
            f"retrieval_recall {score.retrieval_recall:.2f} < "
            f"{thresholds.retrieval_recall:.2f} (not retrieved: {missed})"
        )
    if failures:
        raise AssertionError(
            "eval thresholds not met: " + "; ".join(failures) + f" | {score.summary()}"
        )


def _expected_document_name(question: GoldenQuestion) -> str:
    """The filename of the document expected to support ``question``."""
    from tests.eval.golden import document_by_id

    assert question.supporting_document is not None
    return document_by_id(question.supporting_document).filename


__all__ = [
    "Thresholds",
    "aggregate",
    "assert_meets",
    "score_item",
]
