"""Compression-regression eval — post-compaction answers stay grounded (#415).

The ADR-0016 §3.1/§3.2 gate: when in-answer compaction digests tool results, the
evidence behind the answer's citations must remain visible to the synthesizing
model, and the produced answer must still pass the harness's groundedness +
citation-correctness metrics. Offline and deterministic: the transcript is
compacted with the REAL :func:`~app.llm.context.fit_transcript`, and a faithful
"grounded model" simulation asserts a fact ONLY when its supporting evidence is
present in the post-compaction transcript (and honestly refuses otherwise) — so
the metrics score exactly what a grounding-faithful model could do with the
compacted context.

The negative half proves the gate has teeth (#431 review, blocker 2): with the
snippet re-embedding disabled (the pre-fix head-truncation behavior), the cited
evidence vanishes from the model's view, the faithful model must refuse while
citations are still persisted — and :func:`~tests.eval.metrics.groundedness`
FAILS (a refusal carrying citations). Reverting snippet-aware compaction turns
this suite red.
"""

from __future__ import annotations

import uuid

from app.domain.llm import ChatMessage, Role, ToolCall
from app.llm.context import ContextConfig, fit_transcript
from tests.eval.metrics import (
    ObservedAnswer,
    ObservedPassage,
    citation_correctness,
    groundedness,
)

# The golden evidence: the supporting passage sits BEYOND the digest head of its
# tool result, so naive head-truncation loses it while snippet-aware compaction
# must re-embed it verbatim.
_EVIDENCE = "The 2024 standard deduction for single filers is $14,600."
_DOC_NAME = "taxes.pdf"


def _transcript() -> list[ChatMessage]:
    """A grown answer transcript: one cited search result + uncited bloat."""
    filler = "notes " * 80  # ~480 chars of pre-amble inside the cited result
    cited_content = f"[1] {_DOC_NAME} (chunk c1, chars 0-500):\n{filler}{_EVIDENCE}"
    return [
        ChatMessage(role=Role.SYSTEM, content="You are a grounded assistant."),
        ChatMessage(role=Role.USER, content="What is the 2024 standard deduction?"),
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "deduction"}),),
        ),
        ChatMessage(role=Role.TOOL, content=cited_content, tool_call_id="c1", name="search_text"),
        ChatMessage(
            role=Role.ASSISTANT,
            content="",
            tool_calls=(ToolCall(id="c2", name="get_document", arguments={"document_id": "x"}),),
        ),
        ChatMessage(role=Role.TOOL, content="D" * 1500, tool_call_id="c2", name="get_document"),
    ]


def _compact(cited_snippets: dict[str, tuple[str, ...]]) -> list[ChatMessage]:
    """Run the REAL compaction over the golden transcript, forcing digestion."""
    return fit_transcript(
        _transcript(),
        model="fake/model",
        config=ContextConfig(
            fallback_max_input_tokens=1024 + 1400,  # budget 1400 ⇒ must compact BOTH results
            output_headroom_tokens=0,
            compaction_digest_chars=100,
            compaction_chunk_size=2,
        ),
        counter=len,
        max_input_resolver=lambda _m: None,
        cited_snippets=cited_snippets,
    )


def _faithful_model_answer(transcript: list[ChatMessage]) -> str:
    """What a grounding-faithful model produces from THIS transcript.

    Asserts the fact only when its supporting evidence is visible; otherwise it
    honestly refuses — the behavioral contract the grounded prompt demands and
    the strongest simulation an offline eval can make.
    """
    visible = any(_EVIDENCE in m.content for m in transcript)
    if visible:
        return "The 2024 standard deduction for single filers is $14,600."
    return "I couldn't find the standard deduction amount in your sources."


def _observed(answer_text: str, owner: uuid.UUID) -> ObservedAnswer:
    """The run's observation: the cited passage was retrieved AND persisted."""
    passage = ObservedPassage(
        document_id=uuid.uuid4(),
        document_name=_DOC_NAME,
        text=_EVIDENCE,
        owner_id=owner,
    )
    return ObservedAnswer(
        retrieved=(passage,),
        citations=(passage,),  # the runtime records citations at retrieval time
        answer_text=answer_text,
        asked_by=owner,
    )


def test_post_compaction_answer_stays_grounded_and_citation_correct() -> None:
    """The gate (positive): snippet-aware compaction keeps the cited evidence in
    the model's view, so the faithful answer asserts the fact and both metrics
    pass at threshold 1.0."""
    owner = uuid.uuid4()
    compacted = _compact({"c1": (_EVIDENCE,)})

    # Compaction genuinely happened (both results digested)…
    assert sum("truncated to fit" in m.content for m in compacted) >= 1
    # …and the evidence-visibility invariant holds: the cited snippet is still
    # verbatim in the model-facing transcript.
    assert any(_EVIDENCE in m.content for m in compacted)

    observed = _observed(_faithful_model_answer(compacted), owner)
    assert groundedness(observed) is True
    assert (
        citation_correctness(
            observed, expected_passage=_EVIDENCE, expected_document_name=_DOC_NAME
        )
        is True
    )


def test_gate_fails_when_compaction_drops_cited_evidence() -> None:
    """The gate (negative, #431 blocker 2): with snippet re-embedding disabled
    (the pre-fix head-truncation), the evidence leaves the model's view, the
    faithful model must refuse while citations persist — and groundedness FAILS.
    This is what turns the suite red if snippet-aware compaction regresses."""
    owner = uuid.uuid4()
    # Simulate the old behavior: compact WITHOUT any cited-snippet protection.
    head_truncated = _compact({})

    # The evidence really is gone from the model's view…
    assert not any(_EVIDENCE in m.content for m in head_truncated)

    observed = _observed(_faithful_model_answer(head_truncated), owner)
    # …so the faithful model refused while citations were persisted — exactly the
    # ungrounded shape the metric must catch (refusal + citations ⇒ FAIL).
    assert groundedness(observed) is False
