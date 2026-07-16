"""Context-assembler tests — token budgets, degrade order, determinism (#410).

Pure offline: the counter and max-input seams are injected fakes (1 token per
character; a fixed window), so every boundary is exact and no LiteLLM import
happens. The assembler is the ADR-0016 §1 seam the runtime builds its prompt
through; these tests pin its acceptance criteria:

* AC-1 — history over budget trims OLDEST-first (whole messages); newest
  survive and the trim is logged/reported;
* AC-2 — assembly is deterministic and order-preserving
  (system → memory → summary → history → question) with identical inputs;
* AC-3 — an unknown model (resolver → None) uses the configured fallback
  budget and never crashes;
* the fixed segments alone exceeding the budget refuse with the typed 422
  (``context_too_large``) rather than a provider crash.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.domain.llm import ChatMessage, Role, ToolSpec
from app.llm.context import ContextBudget, ContextConfig, assemble_context

# 1 token per character: exact, deterministic budgets in tests.
_CHAR_COUNTER = len


def _msg(role: Role, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def _assemble(
    *,
    history: list[ChatMessage],
    max_input: int | None,
    system: str = "SYS",
    question: str = "Q",
    tools: tuple[ToolSpec, ...] = (),
    fallback: int = 10_000,
    headroom: int = 0,
) -> ContextBudget:
    return assemble_context(
        model="fake/model",
        system_prompt=system,
        history=history,
        question=question,
        tools=tools,
        config=ContextConfig(
            fallback_max_input_tokens=fallback, output_headroom_tokens=headroom
        ),
        counter=_CHAR_COUNTER,
        max_input_resolver=lambda _model: max_input,
    )


def test_within_budget_keeps_everything_in_order() -> None:
    """AC-2: under budget, assembly is the identity — system, history, question."""
    history = [
        _msg(Role.USER, "first question"),
        _msg(Role.ASSISTANT, "first answer"),
        _msg(Role.USER, "second question"),
        _msg(Role.ASSISTANT, "second answer"),
    ]
    out = _assemble(history=history, max_input=50_000)

    assert out.dropped_history_messages == 0
    assert [m.role for m in out.messages] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
    ]
    assert out.messages[0].content == "SYS"
    assert [m.content for m in out.messages[1:-1]] == [m.content for m in history]
    assert out.messages[-1].content == "Q"
    # Roomy budget ⇒ default retrieval k (unchanged from the runtime's historical 6).
    assert out.retrieval_k == 6


def test_over_budget_drops_oldest_first_and_reports() -> None:
    """AC-1: the newest turns survive; the drop is a clean oldest-first cut."""
    # Window 1200 + safety margin 1024 ⇒ budget 1200. Fixed = system 3 +
    # question 1 + 2*4 overhead = 12 ⇒ ~1188 for history. Each message costs
    # 50 content + 4 overhead = 54; 22 fit (1188), so make history longer than
    # that to force a drop — use large messages instead for a crisp boundary.
    history = [
        _msg(Role.USER, "u" * 600),
        _msg(Role.ASSISTANT, "a" * 600),
        _msg(Role.USER, "b" * 600),
        _msg(Role.ASSISTANT, "c" * 600),
    ]
    # budget 1200; fixed 12; remaining 1188; each msg 604 ⇒ exactly one fits.
    out = _assemble(history=history, max_input=1200 + 1024)

    assert out.dropped_history_messages == 3
    kept = out.messages[1:-1]
    assert [m.content[0] for m in kept] == ["c"]  # only the NEWEST survives
    # Deterministic: an identical second run assembles the identical prompt.
    again = _assemble(history=history, max_input=1200 + 1024)
    assert [m.content for m in again.messages] == [m.content for m in out.messages]


def test_unknown_model_uses_fallback_budget() -> None:
    """AC-3: resolver → None ⇒ the configured fallback window, no crash."""
    history = [_msg(Role.USER, "x" * 300)]
    generous = _assemble(history=history, max_input=None, fallback=50_000 + 1024)
    assert generous.dropped_history_messages == 0

    # Shrink the fallback so the single history message no longer fits.
    tighter = _assemble(history=history, max_input=None, fallback=200 + 1024)
    assert tighter.dropped_history_messages == 1


def test_fixed_segments_over_budget_refuse_typed() -> None:
    """A question+system+tools that cannot fit is a typed 422, never a provider crash."""
    with pytest.raises(ValidationError) as excinfo:
        _assemble(
            history=[],
            max_input=1024 + 64,  # margin swallows almost everything
            system="s" * 200,
            question="q" * 200,
        )
    assert excinfo.value.code == "context_too_large"


def test_tool_schemas_spend_the_budget() -> None:
    """Advertised tool specs count against the window (they ride the request too)."""
    # budget 300 (max_input 1324 − 1024 margin). One history msg = 104 tokens.
    # Without tools: fixed = 3+1+0+8 = 12, remaining 288 ≥ 104 ⇒ fits.
    # With the fat tool: schema "d"*200 + name + serialized params pushes fixed
    # to ~245, remaining ~55 < 104 ⇒ the message no longer fits (dropped).
    history = [_msg(Role.USER, "h" * 100)]
    fat_tool = ToolSpec(
        name="t",
        description="d" * 200,
        parameters={"type": "object", "properties": {}},
    )
    without = _assemble(history=history, max_input=1024 + 300)
    with_tools = _assemble(history=history, max_input=1024 + 300, tools=(fat_tool,))
    assert without.dropped_history_messages == 0
    assert with_tools.dropped_history_messages == 1


def test_tight_budget_shrinks_retrieval_k() -> None:
    """Degrade order step 3: a nearly-full window tells the run to retrieve fewer."""
    # Fill history so almost nothing remains after it — retrieval_k should drop.
    history = [_msg(Role.USER, "z" * 1000)]
    out = _assemble(history=history, max_input=1100 + 1024)  # budget 1100, msg 1004
    assert out.dropped_history_messages == 0
    assert out.retrieval_k == 3  # tight ⇒ fewer passages
