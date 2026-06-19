"""Eval metrics — groundedness, citation-correctness, retrieval-recall (#29).

Pure scoring functions over the **observed** answer of one golden item: the
retrieved passages, the citations the runtime attached, and the answer text.
They take simple value objects (built either from the offline faked run or the
live run) and return per-item booleans/ratios; :mod:`tests.eval.scoring`
aggregates them into a report and asserts thresholds.

The three metrics map one-to-one onto the mission concerns (ADR-0003 §9,
spec 0004 INV-2/INV-3):

* **groundedness** — does every asserted fact carry a citation? An answer that
  states facts (i.e. is *not* the honest "couldn't find it") but carries **zero
  citations** is ungrounded and FAILS. The honest refusal is grounded *because*
  it asserts nothing — it correctly carries no citation.
* **citation-correctness** — does each citation point at the expected supporting
  source, and (the INV-3 negative) does it point only at a **permitted** passage?
  A citation whose passage does not overlap the expected supporting passage, or
  that references a document outside the asking user's allow-set, FAILS.
* **retrieval-recall** — was the expected supporting passage actually retrieved?
  (The upstream signal: a citation can only be correct if retrieval surfaced the
  right passage in the first place.)

Everything here is I/O-free and deterministic so the metrics themselves are unit
tested (``test_eval_metrics.py``) — the harness's own correctness is proven, not
assumed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ObservedPassage:
    """A passage the run surfaced (retrieved or cited), with its provenance.

    ``document_id`` / ``document_name`` identify the source; ``text`` is the
    passage text used for the substring-overlap match against the expected
    supporting passage. ``owner_id`` is the document owner — carried so the
    INV-3 permission check can assert the passage was the asking user's.
    """

    document_id: UUID
    document_name: str
    text: str
    owner_id: UUID


@dataclass(frozen=True, slots=True)
class ObservedAnswer:
    """What one golden question produced — the inputs every metric scores.

    ``retrieved`` are the passages retrieval returned for the turn;
    ``citations`` are the passages the runtime actually cited (a subset of what
    was retrieved, by construction — see chat_runtime); ``answer_text`` is the
    final assistant message; ``asked_by`` is the user id of the asker (so a
    citation's ``owner_id`` can be checked against it for INV-3).
    """

    retrieved: tuple[ObservedPassage, ...]
    citations: tuple[ObservedPassage, ...]
    answer_text: str
    asked_by: UUID


# A short list of phrases that mark an honest "I couldn't find it" answer. An
# answer matching one of these asserts no fact, so it is grounded with zero
# citations (AC-3). Kept lowercase; matched as a substring of the lowered answer.
_REFUSAL_MARKERS: tuple[str, ...] = (
    "couldn't find",
    "could not find",
    "couldn't locate",
    "i don't know",
    "i do not know",
    "no information",
    "not find anything",
    "unable to find",
)


def is_refusal(answer_text: str) -> bool:
    """True iff ``answer_text`` reads as an honest "couldn't find it" answer.

    Used by groundedness: a refusal asserts no fact, so a zero-citation refusal
    is *correctly* grounded rather than an ungrounded claim.
    """
    lowered = answer_text.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def _normalize(text: str) -> str:
    """Collapse whitespace + lowercase so substring overlap is robust.

    Chunking may insert/trim whitespace at boundaries; normalising both sides
    before the overlap check keeps the metric from failing on cosmetic spacing
    differences while still requiring genuine textual overlap.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def passages_overlap(a: str, b: str) -> bool:
    """True iff one normalised passage contains the other (non-trivially).

    The substring-overlap match used to decide whether an observed passage
    "matches" the expected supporting passage without pinning exact chunk
    boundaries: a chunk may be larger than the expected passage (contains it) or
    the expected passage may span two chunks (the chunk is contained in it). A
    blank passage never overlaps (guards the refusal case from a false match).
    """
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def retrieval_recall(observed: ObservedAnswer, *, expected_passage: str) -> bool:
    """Was ``expected_passage`` among the retrieved passages? (recall, per item)

    For an answerable question this asks the upstream question: did hybrid
    retrieval surface the supporting passage at all? For the unanswerable case
    the caller does not score recall (there is no expected passage).
    """
    return any(passages_overlap(p.text, expected_passage) for p in observed.retrieved)


def citation_correctness(
    observed: ObservedAnswer,
    *,
    expected_passage: str,
    expected_document_name: str,
) -> bool:
    """Do the citations point at the expected source — and only at permitted ones?

    Two conditions, both required (INV-3):

    1. **Permitted** — every cited passage must be owned by the asking user
       (``owner_id == asked_by``). A citation to a passage the user could not
       retrieve is a hard fail (the INV-3 negative) regardless of text overlap.
    2. **Correct** — at least one citation must match the expected supporting
       passage *and* come from the expected document, i.e. the answer cited the
       right evidence, not merely *some* permitted passage.

    A turn with no citations fails correctness for an answerable question (there
    is no correct citation present); the refusal case is scored separately.
    """
    # 1. INV-3 permission: no citation may reference a non-permitted passage.
    for cit in observed.citations:
        if cit.owner_id != observed.asked_by:
            return False
    # 2. At least one citation matches the expected supporting passage + document.
    for cit in observed.citations:
        if cit.document_name == expected_document_name and passages_overlap(
            cit.text, expected_passage
        ):
            return True
    return False


def groundedness(observed: ObservedAnswer) -> bool:
    """Does every asserted fact carry a citation? (the core quality guard)

    Operationalised as: an answer that asserts facts must carry at least one
    citation; an honest refusal (asserts nothing) must carry **zero** citations.
    So:

    * a fact-asserting answer with **no** citation → ungrounded (FAIL) — the
      "answer with an unsupported claim" negative;
    * a refusal with citations → also a FAIL (it claims sources it shouldn't);
    * a refusal with zero citations → grounded (PASS);
    * a fact-asserting answer with ≥1 citation → grounded (PASS).

    Citation *correctness* (right source, permitted) is a separate metric; this
    one only asks whether claims are *backed at all*.
    """
    if is_refusal(observed.answer_text):
        return len(observed.citations) == 0
    # A non-refusal answer asserts facts — it must carry at least one citation.
    return len(observed.citations) > 0


# --- Aggregation ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemScore:
    """The per-question metric outcome (feeds the aggregate report)."""

    qid: str
    answerable: bool
    grounded: bool
    citation_correct: bool
    retrieved: bool  # recall; only meaningful for answerable items


@dataclass(frozen=True, slots=True)
class AggregateScore:
    """Aggregate metrics over the golden set (the numbers thresholds compare to).

    ``groundedness`` and ``citation_correctness`` are computed over **all** items
    (the refusal case counts: it must be grounded with no citation and is treated
    as citation-correct because it correctly cites nothing). ``retrieval_recall``
    is computed over the **answerable** items only (recall is undefined for a
    question with no expected passage).
    """

    items: tuple[ItemScore, ...] = field(default_factory=tuple)

    @property
    def groundedness(self) -> float:
        return _ratio(s.grounded for s in self.items)

    @property
    def citation_correctness(self) -> float:
        return _ratio(s.citation_correct for s in self.items)

    @property
    def retrieval_recall(self) -> float:
        answerable = [s for s in self.items if s.answerable]
        if not answerable:
            return 1.0
        return _ratio(s.retrieved for s in answerable)

    def summary(self) -> str:
        return (
            f"groundedness={self.groundedness:.2f} "
            f"citation_correctness={self.citation_correctness:.2f} "
            f"retrieval_recall={self.retrieval_recall:.2f} "
            f"(n={len(self.items)})"
        )


def _ratio(flags: object) -> float:
    """Fraction of truthy flags in an iterable (1.0 for an empty iterable)."""
    values = list(flags)  # type: ignore[call-overload]
    if not values:
        return 1.0
    return sum(1 for v in values if v) / len(values)


__all__ = [
    "AggregateScore",
    "ItemScore",
    "ObservedAnswer",
    "ObservedPassage",
    "citation_correctness",
    "groundedness",
    "is_refusal",
    "passages_overlap",
    "retrieval_recall",
]
