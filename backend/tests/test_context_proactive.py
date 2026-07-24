"""Proactive tool-result compaction + incremental token fitting (#491).

Pure offline, injected seams (1 token per character; a fixed window), so every
boundary is exact and no LiteLLM import happens. These pin the two #491 context
changes:

* **Proactive compaction (AC-1/AC-6).** ``fit_transcript`` shrinks a tool
  result to a bounded head digest ONCE it is both *superseded* (a later tool
  group exists) and *citation-backed* — independent of the budget check, which
  a frontier window never trips. The newest tool group is NEVER touched (AC-6:
  the evidence a pending tool call is about to reference is preserved), and an
  uncited superseded result is left verbatim.

* **Incremental fitting (AC-3).** A memoizing counter makes a turn's fit cost
  O(new messages), not O(transcript): the expensive tokeniser fires only for
  the messages a turn actually appends, never re-tokenising the whole grown
  transcript every turn.
"""

from __future__ import annotations

from app.domain.llm import ChatMessage, Role, ToolCall, ToolSpec
from app.llm.context import (
    ContextConfig,
    fit_transcript,
    memoizing_token_counter,
)

_CHAR_COUNTER = len


def _msg(role: Role, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def _tool_call(call_id: str) -> ChatMessage:
    return ChatMessage(
        role=Role.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=call_id, name="search_text", arguments={"q": call_id}),),
    )


def _tool_result(call_id: str, content: str) -> ChatMessage:
    return ChatMessage(role=Role.TOOL, content=content, tool_call_id=call_id, name="search_text")


def _fit_proactive(
    messages: list[ChatMessage],
    *,
    cited_snippets: dict[str, tuple[str, ...]] | None,
    digest_chars: int = 100,
    max_input: int = 10_000_000,
    enabled: bool = True,
) -> list[ChatMessage]:
    """Fit under a huge budget so ONLY the budget-independent proactive pass fires."""
    return fit_transcript(
        messages,
        model="fake/model",
        tools=(),
        config=ContextConfig(
            fallback_max_input_tokens=1_000_000,
            output_headroom_tokens=0,
            compaction_digest_chars=digest_chars,
            proactive_compaction_enabled=enabled,
        ),
        counter=_CHAR_COUNTER,
        max_input_resolver=lambda _m: max_input,
        cited_snippets=cited_snippets,
    )


def _three_searches() -> list[ChatMessage]:
    return [
        _msg(Role.SYSTEM, "SYS"),
        _msg(Role.USER, "question"),
        _tool_call("c1"),
        _tool_result("c1", "A" * 3600),
        _tool_call("c2"),
        _tool_result("c2", "B" * 3600),
        _tool_call("c3"),
        _tool_result("c3", "C" * 3600),
    ]


def test_proactive_digests_superseded_cited_results_and_spares_the_newest() -> None:
    """AC-1 + AC-6: under a frontier (never-over) budget, the superseded search
    results shrink to a head digest, while the NEWEST result — the one a pending
    tool call is about to reference — stays verbatim."""
    messages = _three_searches()
    fitted = _fit_proactive(
        messages,
        cited_snippets={"c1": ("a-snip",), "c2": ("b-snip",), "c3": ("c-snip",)},
    )
    by_id = {m.tool_call_id: m for m in fitted if m.role is Role.TOOL}

    # The two SUPERSEDED results are digested (short head + the truncation marker).
    assert "truncated to fit" in by_id["c1"].content
    assert "truncated to fit" in by_id["c2"].content
    assert len(by_id["c1"].content) < 3600
    assert len(by_id["c2"].content) < 3600
    # AC-6: the newest group's result is never touched — full 3600 chars.
    assert by_id["c3"].content == "C" * 3600


def test_proactive_preserves_cited_snippet_beyond_digest_chars() -> None:
    """INV-3 (BE-2): a cited snippet that sits BEYOND ``digest_chars`` must survive
    the proactive digest verbatim. A superseded, cited result IS digested (its body
    is far larger than head + snippet), but the cited passage the answer pointed at
    is re-embedded, so the evidence the citation resolves to stays in the
    model-visible prompt — a blind head truncation would have dropped it."""
    cited = "CITED-ALPHA-" + "x" * 400  # a distinctive passage well past the 100-char head
    body = "A" * 3600 + "\n" + cited  # the cited passage lives at the tail, beyond digest_chars
    messages = [
        _msg(Role.SYSTEM, "SYS"),
        _msg(Role.USER, "question"),
        _tool_call("c1"),
        _tool_result("c1", body),  # superseded + cited
        _tool_call("c2"),
        _tool_result("c2", "B" * 3600),  # newest → protected
    ]
    fitted = _fit_proactive(
        messages, cited_snippets={"c1": (cited,), "c2": ("b",)}, digest_chars=100
    )
    by_id = {m.tool_call_id: m for m in fitted if m.role is Role.TOOL}
    # The cited evidence beyond the head survives byte-for-byte (INV-3) …
    assert cited in by_id["c1"].content
    # … while the result still shrank, so proactive compaction really did fire.
    assert "truncated to fit" in by_id["c1"].content
    assert len(by_id["c1"].content) < len(body)
    # AC-6: the newest result is untouched.
    assert by_id["c2"].content == "B" * 3600


def test_proactive_leaves_cited_result_verbatim_when_snippet_dominates() -> None:
    """INV-3 (BE-2) idempotence corollary: when re-embedding the cited snippet
    cannot reduce the wire cost — here the snippet IS essentially the whole body —
    the message is left VERBATIM rather than blindly truncated, so a superseded
    result is never compacted at the price of dropping its cited evidence."""
    body = "CITED-WHOLE-" + "y" * 3600  # the entire body is the cited passage, beyond 100 chars
    messages = [
        _msg(Role.SYSTEM, "SYS"),
        _msg(Role.USER, "question"),
        _tool_call("c1"),
        _tool_result("c1", body),  # superseded + cited, snippet == body
        _tool_call("c2"),
        _tool_result("c2", "B" * 3600),  # newest → protected
    ]
    fitted = _fit_proactive(
        messages, cited_snippets={"c1": (body,), "c2": ("b",)}, digest_chars=100
    )
    by_id = {m.tool_call_id: m for m in fitted if m.role is Role.TOOL}
    # Re-embedding the full snippet can't beat the original cost → left verbatim
    # (the cited evidence is never dropped just to force a shrink).
    assert by_id["c1"].content == body
    assert by_id["c2"].content == "B" * 3600


def test_proactive_is_off_by_default_in_the_assembler() -> None:
    """The pure assembler default is conservative: proactive compaction is opt-in
    (the runtime turns it on via Settings), so an under-budget fit is the identity
    even with cited superseded results — no surprise mutation for existing callers."""
    messages = _three_searches()
    fitted = _fit_proactive(
        messages,
        cited_snippets={"c1": ("a",), "c2": ("b",), "c3": ("c",)},
        enabled=False,
    )
    assert [m.content for m in fitted] == [m.content for m in messages]


def test_proactive_never_compacts_the_only_tool_group() -> None:
    """AC-6: a single (hence newest) tool group is the evidence the next turn
    acts on — it is never superseded, so proactive never touches it."""
    messages = [
        _msg(Role.SYSTEM, "SYS"),
        _msg(Role.USER, "question"),
        _tool_call("c1"),
        _tool_result("c1", "A" * 3600),
    ]
    fitted = _fit_proactive(messages, cited_snippets={"c1": ("a",)})
    assert fitted[-1].content == "A" * 3600


def test_proactive_leaves_uncited_superseded_results_verbatim() -> None:
    """Only results BOTH superseded AND represented in citations compact — an
    uncited superseded result may still hold evidence no citation has captured,
    so it rides verbatim (the directive's 'AND already represented in citations')."""
    messages = [
        _msg(Role.SYSTEM, "SYS"),
        _msg(Role.USER, "question"),
        _tool_call("c1"),
        _tool_result("c1", "A" * 3600),  # superseded, UNcited
        _tool_call("c2"),
        _tool_result("c2", "B" * 3600),  # newest, cited
    ]
    fitted = _fit_proactive(messages, cited_snippets={"c2": ("b",)})
    by_id = {m.tool_call_id: m for m in fitted if m.role is Role.TOOL}
    assert by_id["c1"].content == "A" * 3600  # uncited superseded → verbatim
    assert by_id["c2"].content == "B" * 3600  # newest → verbatim


def test_proactive_does_not_break_citation_resolution_records() -> None:
    """AC-5: proactive compaction only rewrites the transcript COPY of evidence;
    a compacted result still carries its tool_call_id and role, so nothing about
    the citation records (kept independently at retrieval time) changes — the
    digest is a superset marker, never a citation."""
    messages = _three_searches()
    fitted = _fit_proactive(messages, cited_snippets={"c1": ("a",), "c2": ("b",), "c3": ("c",)})
    tool_msgs = [m for m in fitted if m.role is Role.TOOL]
    # Every tool result keeps its identity (call id + role) after compaction.
    assert [m.tool_call_id for m in tool_msgs] == ["c1", "c2", "c3"]
    assert all(m.role is Role.TOOL for m in tool_msgs)


def test_proactive_is_idempotent_across_turns() -> None:
    """A result compacted last turn is at/under the cap, so re-fitting the grown
    transcript next turn does not re-compact it (strict cost guard) — the prefix
    stays stable, preserving provider cache hits."""
    messages = _three_searches()
    cited = {"c1": ("a",), "c2": ("b",), "c3": ("c",)}
    once = _fit_proactive(messages, cited_snippets=cited)
    # Grow by one more search turn and refit — c3 is now superseded and compacts,
    # but c1/c2 (already digested) are untouched.
    grown = [*once, _tool_call("c4"), _tool_result("c4", "D" * 3600)]
    twice = _fit_proactive(grown, cited_snippets={**cited, "c4": ("d",)})
    by_id_once = {m.tool_call_id: m.content for m in once if m.role is Role.TOOL}
    by_id_twice = {m.tool_call_id: m.content for m in twice if m.role is Role.TOOL}
    assert by_id_twice["c1"] == by_id_once["c1"]  # unchanged (stable prefix)
    assert by_id_twice["c2"] == by_id_once["c2"]
    assert "truncated to fit" in by_id_twice["c3"]  # now superseded → digested
    assert by_id_twice["c4"] == "D" * 3600  # newest → verbatim


def test_memoizing_counter_makes_fit_incremental_per_turn() -> None:
    """AC-3: over a 10-turn synthetic transcript the tokeniser is invoked only for
    a turn's NEW messages, never re-tokenising the whole transcript. Asserts the
    COUNT of underlying token-count computations (not wall-clock)."""
    base_calls: list[str] = []

    def base(text: str) -> int:
        base_calls.append(text)
        return len(text)

    memo = memoizing_token_counter(base)
    tools = (ToolSpec(name="search_text", description="d", parameters={"type": "object"}),)
    messages = [_msg(Role.SYSTEM, "SYS"), _msg(Role.USER, "question")]
    per_turn_new: list[int] = []
    for t in range(10):
        messages = [
            *messages,
            _tool_call(f"c{t}"),
            _tool_result(f"c{t}", f"result {t} " * 50),
        ]
        before = len(base_calls)
        fitted = fit_transcript(
            messages,
            model="fake/model",
            tools=tools,
            counter=memo,
            max_input_resolver=lambda _m: 10_000_000,
        )
        assert len(fitted) == len(messages)  # under budget → identity
        per_turn_new.append(len(base_calls) - before)

    # Turn 0 tokenises SYS, question, the tools block, and its 2 new messages = 5
    # distinct fragments. EVERY later turn tokenises ONLY its 2 new messages — the
    # system/question/tools/prior-turns are cache hits. That is O(new), not
    # O(transcript): a full re-tokenisation would grow every turn.
    assert per_turn_new[0] == 5
    assert all(count == 2 for count in per_turn_new[1:])
    assert sum(per_turn_new) == 5 + 2 * 9


def test_memoizing_counter_returns_identical_counts() -> None:
    """Memoisation is transparent: the wrapped counter returns the base count,
    only skipping the recompute — so token budgets are byte-identical."""
    calls: list[str] = []

    def base(text: str) -> int:
        calls.append(text)
        return len(text.encode("utf-8"))

    memo = memoizing_token_counter(base)
    assert memo("hello") == 5
    assert memo("hello") == 5  # cache hit — same value
    assert memo("héllo") == len("héllo".encode())
    assert calls == ["hello", "héllo"]  # the repeat did NOT re-invoke base
