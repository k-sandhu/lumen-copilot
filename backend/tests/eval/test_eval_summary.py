"""Summary-compression eval — long sessions stay grounded (#416, ADR-0016 §3.2).

The rolling-summary analogue of the #415 compression gate: when older turns are
replaced by [summary vN] + the IDs-only evidence digest, a follow-up whose
support lives behind the digest must still yield a grounded, correctly-cited
answer. Offline and deterministic: the context is assembled with the REAL
:func:`~app.llm.context.assemble_context` (the summary segment production code),
and the faithful-model simulation re-fetches evidence ONLY when the digest line
is visible — the strongest offline claim about what a grounding-faithful model
can do with the compacted session.

The negative half proves the gate has teeth: with the digest absent (evidence
dropped instead of carried), the faithful model cannot target the document,
refuses while citations were persisted — and groundedness FAILS.
"""

from __future__ import annotations

import uuid

from app.llm.context import ContextConfig, assemble_context
from tests.eval.metrics import (
    ObservedAnswer,
    ObservedPassage,
    citation_correctness,
    groundedness,
)

_EVIDENCE = "The 2024 standard deduction for single filers is $14,600."
_DOC_NAME = "taxes.pdf"
_DOC_ID = uuid.uuid4()


def _assembled(evidence_lines: tuple[str, ...]) -> list[str]:
    """The REAL assembler over a summarized long session; returns message texts."""
    budget = assemble_context(
        model="fake/model",
        system_prompt="You are a grounded assistant.",
        history=[],  # older turns live in the summary, newer ones are empty here
        question="Expand on the deduction figure from that document.",
        config=ContextConfig(fallback_max_input_tokens=50_000),
        counter=len,
        max_input_resolver=lambda _m: None,
        summary=(
            "Earlier the user asked about 2024 tax rules; the assistant answered "
            f"from {_DOC_NAME} and cited the standard-deduction figure."
        ),
        evidence_lines=evidence_lines,
    )
    return [m.content for m in budget.messages]


def _faithful_model_answer(prompt_texts: list[str]) -> str:
    """Asserts the fact ONLY if it can target the document by id (the digest)."""
    digest_visible = any(str(_DOC_ID) in t for t in prompt_texts)
    if digest_visible:
        # The faithful model fetches by id (get_document) and re-cites.
        return _EVIDENCE
    return "I couldn't find the deduction figure in your sources."


def _observed(answer_text: str, owner: uuid.UUID) -> ObservedAnswer:
    passage = ObservedPassage(
        document_id=_DOC_ID,
        document_name=_DOC_NAME,
        text=_EVIDENCE,
        owner_id=owner,
    )
    return ObservedAnswer(
        retrieved=(passage,),
        citations=(passage,),
        answer_text=answer_text,
        asked_by=owner,
    )


def test_summarized_session_follow_up_stays_grounded_and_cited() -> None:
    """The gate (positive): the digest line carries the doc id, the faithful
    model re-fetches + answers, and both metrics hold at 1.0."""
    owner = uuid.uuid4()
    texts = _assembled(
        (f"{_DOC_NAME} (document_id {_DOC_ID}; cited chunk(s): {uuid.uuid4()})",)
    )
    # The summary segment is really in the assembled prompt…
    assert any("Conversation summary" in t for t in texts)
    observed = _observed(_faithful_model_answer(texts), owner)
    assert groundedness(observed) is True
    assert (
        citation_correctness(
            observed, expected_passage=_EVIDENCE, expected_document_name=_DOC_NAME
        )
        is True
    )


def test_gate_fails_when_the_evidence_digest_is_dropped() -> None:
    """The gate (negative): no digest ⇒ the faithful model cannot target the
    document, refuses while citations persist — groundedness FAILS. Reverting
    evidence carry-forward turns this suite red."""
    owner = uuid.uuid4()
    texts = _assembled(())
    assert not any(str(_DOC_ID) in t for t in texts)
    observed = _observed(_faithful_model_answer(texts), owner)
    assert groundedness(observed) is False
