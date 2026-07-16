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
from app.domain.llm import ChatMessage, Role, ToolCall, ToolSpec
from app.llm.context import (
    ContextBudget,
    ContextConfig,
    assemble_context,
    fit_transcript,
    litellm_max_input_tokens,
)

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
    """Advertised tool specs count against the window — including the full
    OpenAI wire framing, not just name+description+schema (#424 review, finding 3)."""
    # budget 450 (max_input 1474 − 1024 margin). The single history message
    # (wire ≈130) fits without tools, but the fat tool's serialized wire form
    # (the full {"type":"function",...} envelope + the "d"*200 description) spends
    # enough of the budget that the message no longer fits (dropped).
    history = [_msg(Role.USER, "h" * 100)]
    fat_tool = ToolSpec(
        name="t",
        description="d" * 200,
        parameters={"type": "object", "properties": {}},
    )
    without = _assemble(history=history, max_input=1024 + 450)
    with_tools = _assemble(history=history, max_input=1024 + 450, tools=(fat_tool,))
    assert without.dropped_history_messages == 0
    assert with_tools.dropped_history_messages == 1


def test_tight_budget_shrinks_retrieval_k_and_max_k() -> None:
    """Degrade order step 3: a nearly-full window shrinks BOTH the default k and
    the enforceable ceiling (#424 review, finding 3), so even an explicit large k
    is clamped downstream."""
    history = [_msg(Role.USER, "z" * 1000)]
    # budget 1200 (max_input 2224 − 1024): the ~1030-token message fits but leaves
    # only ~100 free (< 15% of budget), so the window is "tight".
    tight = _assemble(history=history, max_input=2224)
    assert tight.dropped_history_messages == 0
    assert tight.retrieval_k == 3
    assert tight.max_k == 3  # the CEILING drops too — clamps explicit k

    roomy = _assemble(history=[_msg(Role.USER, "x")], max_input=50_000)
    assert roomy.retrieval_k == 6
    # Roomy ceiling is the WIDEST tool cap (50, list_documents); each search tool
    # still clamps to its own 20 — so this is inert for search, permissive for list.
    assert roomy.max_k == 50


# --- #424 review, finding 1: the GROWN transcript is re-fit before every turn --


def _fit(
    messages: list[ChatMessage],
    *,
    max_input: int,
    tools: tuple[ToolSpec, ...] = (),
    fallback: int = 10_000,
    headroom: int = 0,
) -> list[ChatMessage]:
    return fit_transcript(
        messages,
        model="fake/model",
        tools=tools,
        config=ContextConfig(fallback_max_input_tokens=fallback, output_headroom_tokens=headroom),
        counter=_CHAR_COUNTER,
        max_input_resolver=lambda _m: max_input,
    )


def test_fit_transcript_within_budget_is_identity() -> None:
    msgs = [
        _msg(Role.SYSTEM, "SYS"),
        _msg(Role.USER, "question"),
        _msg(Role.ASSISTANT, "answer"),
    ]
    assert [m.content for m in _fit(msgs, max_input=50_000)] == [m.content for m in msgs]


def test_fit_transcript_sheds_oldest_history_preserving_live_tail() -> None:
    """A grown transcript over budget drops oldest HISTORY, keeping the system head
    and the current answer's question + tool-call/result tail intact."""
    system = _msg(Role.SYSTEM, "SYS")
    old_hist = [_msg(Role.USER, "old q " * 40), _msg(Role.ASSISTANT, "old a " * 40)]
    question = _msg(Role.USER, "current question")
    # The current answer's live tail: an assistant tool-call + its tool result.
    tool_call = ChatMessage(
        role=Role.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id="c1", name="search_text", arguments={"query": "x"}),),
    )
    tool_result = ChatMessage(
        role=Role.TOOL, content="passage " * 20, tool_call_id="c1", name="search_text"
    )
    messages = [system, *old_hist, question, tool_call, tool_result]

    # budget 600 (1624 − 1024): with full wire counting the live tail (system +
    # question + tool_call + tool_result) is ~400 and each history turn ~270, so
    # BOTH history turns must be shed but the protected tail fits.
    fitted = _fit(messages, max_input=1624)
    contents = [m.content for m in fitted]
    assert "old q " * 40 not in contents  # history shed
    assert "old a " * 40 not in contents
    assert fitted[0].content == "SYS"  # head preserved
    assert fitted[-1].tool_call_id == "c1"  # live tool tail preserved (paired)
    assert any(m.content == "current question" for m in fitted)


def test_fit_transcript_refuses_when_live_tail_alone_overflows() -> None:
    """If even the system head + current live turn exceed budget, refuse with the
    typed context_too_large rather than send an over-budget call (#424 finding 1)."""
    system = _msg(Role.SYSTEM, "SYS")
    question = _msg(Role.USER, "q")
    huge_result = ChatMessage(
        role=Role.TOOL, content="x" * 5000, tool_call_id="c1", name="search_text"
    )
    with pytest.raises(ValidationError) as excinfo:
        _fit([system, question, huge_result], max_input=1024 + 200)
    assert excinfo.value.code == "context_too_large"


# --- #424 review, findings 2/4/5: resolver + heuristic degradation ------------


def test_resolver_ignores_output_only_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """finding 2: a model reporting only max_tokens (an OUTPUT limit) resolves to
    None — the resolver must NOT treat an output limit as the input window."""
    import sys
    import types as _types

    fake_litellm = _types.SimpleNamespace(
        get_model_info=lambda model: {"max_input_tokens": None, "max_tokens": 8192}
    )
    # Inject a fake litellm module so the lazy ``import litellm`` inside the
    # resolver picks it up (models reporting only an output limit → None).
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)  # type: ignore[arg-type]
    assert litellm_max_input_tokens("gemini/only-output-tokens") is None


def test_resolver_none_uses_configured_fallback_not_refusal() -> None:
    """finding 2 (integration): an unknown/output-only model uses the configured
    fallback window, so a trivial prompt is NOT falsely refused as too-large."""
    out = assemble_context(
        model="gemini/only-output-tokens",
        system_prompt="s",
        history=[_msg(Role.USER, "hi")],
        question="q",
        config=ContextConfig(fallback_max_input_tokens=50_000, output_headroom_tokens=0),
        counter=_CHAR_COUNTER,
        max_input_resolver=lambda _m: None,  # resolver says "unknown"
    )
    assert out.input_budget_tokens == 50_000 - 1024
    assert out.dropped_history_messages == 0


def test_conservative_fallback_is_a_byte_upper_bound() -> None:
    """finding 4: the fallback is a UTF-8 BYTE upper bound (≥ real tokens)."""
    import app.llm.context as ctx_mod

    # A CJK string: 3 chars but 9 UTF-8 bytes — the old len//4 would say 0/1,
    # the conservative bound says the byte length (≥ the char count).
    cjk = "日本語"
    assert ctx_mod._conservative_tokens(cjk) == len(cjk.encode("utf-8"))
    assert ctx_mod._conservative_tokens(cjk) >= len(cjk)  # upper bound, not //4


def test_token_counter_degrades_when_litellm_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """finding 5: a litellm token_counter CALL failure degrades to the byte bound."""
    import sys
    import types as _types

    import app.llm.context as ctx_mod

    def _boom(**_kw: object) -> int:
        raise RuntimeError("no tokenizer")

    monkeypatch.setitem(sys.modules, "litellm", _types.SimpleNamespace(token_counter=_boom))  # type: ignore[arg-type]
    counter = ctx_mod.litellm_token_counter("some/model")
    assert counter("") == 0
    assert counter("日本語") == len("日本語".encode())


def test_token_counter_degrades_when_litellm_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """finding 5: a genuine ``import litellm`` FAILURE (not just a call failure)
    degrades to the conservative byte bound — the import is inside the guard."""
    import sys

    import app.llm.context as ctx_mod

    # sys.modules["litellm"] = None makes ``import litellm`` raise ImportError.
    monkeypatch.setitem(sys.modules, "litellm", None)
    counter = ctx_mod.litellm_token_counter("some/model")
    assert counter("hello") == len(b"hello")
    # The resolver seam degrades the same way — unknown window ⇒ None ⇒ fallback.
    assert ctx_mod.litellm_max_input_tokens("some/model") is None


def test_estimate_counts_tool_call_arguments_not_just_content() -> None:
    """#424 re-review, new finding 1: a message's tool_call arguments/ids/name are
    counted, so a big tool ARGUMENT is not radically under-counted (content=empty)."""
    from app.llm.context import estimate_message_tokens

    plain = ChatMessage(role=Role.ASSISTANT, content="")
    with_big_args = ChatMessage(
        role=Role.ASSISTANT,
        content="",  # NO content — the weight is entirely in the arguments
        tool_calls=(
            ToolCall(id="c1", name="run_python", arguments={"code": "X" * 4000}),
        ),
    )
    plain_cost = estimate_message_tokens([plain], counter=_CHAR_COUNTER)
    big_cost = estimate_message_tokens([with_big_args], counter=_CHAR_COUNTER)
    # The 4000-char argument dominates — the estimate must reflect it, not ~0.
    assert big_cost > 4000
    assert big_cost > plain_cost + 4000


def test_fit_transcript_refuses_on_oversized_tool_argument() -> None:
    """#424 re-review, new finding 1: an oversized tool ARGUMENT (not content) in
    the live tail is caught — the guard refuses rather than sending it."""
    system = _msg(Role.SYSTEM, "SYS")
    question = _msg(Role.USER, "q")
    huge_arg_call = ChatMessage(
        role=Role.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id="c1", name="run_python", arguments={"code": "Y" * 6000}),),
    )
    with pytest.raises(ValidationError) as excinfo:
        _fit([system, question, huge_arg_call], max_input=1024 + 500)
    assert excinfo.value.code == "context_too_large"


def test_more_than_twenty_short_turns_all_fit() -> None:
    """#424 re-review, major 2: with a roomy budget, far more than the old
    fixed-20 history turns are retained (the assembler token-budgets, not counts)."""
    history = [_msg(Role.USER if i % 2 == 0 else Role.ASSISTANT, f"turn {i}") for i in range(40)]
    out = _assemble(history=history, max_input=200_000)
    assert out.dropped_history_messages == 0
    # All 40 short turns survive (system + 40 + question).
    assert len(out.messages) == 42


def test_fit_transcript_never_orphans_a_tool_pair_in_history() -> None:
    """#424 third re-review, moderate 3: shedding is tool-pair-aware — a history
    tool result is never left without its call. Given a plain turn followed by a
    tool-call/result pair in history, over budget, the plain turn is shed but the
    pair stays intact (or the whole thing refuses); no orphaned tool_call_id."""
    system = _msg(Role.SYSTEM, "SYS")
    plain = _msg(Role.USER, "p" * 300)  # sheddable
    hist_call = ChatMessage(
        role=Role.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id="h1", name="search_text", arguments={"query": "z"}),),
    )
    hist_result = ChatMessage(
        role=Role.TOOL, content="r" * 100, tool_call_id="h1", name="search_text"
    )
    question = _msg(Role.USER, "current")
    messages = [system, plain, hist_call, hist_result, question]

    # Budget that forces shedding the plain turn but keeps the rest.
    fitted = _fit(messages, max_input=1024 + 500)
    # The plain turn is gone; the tool pair is intact and adjacent.
    assert not any(m.content.startswith("p" * 10) for m in fitted)
    call_ids = {tc.id for m in fitted for tc in m.tool_calls}
    result_ref_ids = {m.tool_call_id for m in fitted if m.tool_call_id is not None}
    # Every retained tool result still has its call present — no orphan.
    assert result_ref_ids <= call_ids
