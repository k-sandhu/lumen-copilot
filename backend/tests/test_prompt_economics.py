"""AC-1 offline token-economics harness for a scripted 3-tool answer (#491).

We cannot spend real provider tokens here, so this does the next best *real*
thing: it assembles the EXACT prompts a scripted 3-tool grounded answer sends —
the real ``GROUNDED_SYSTEM_PROMPT``, the real default tool schemas, and
realistically sized rendered search results — and COUNTS the cumulative input
tokens with the SAME tokeniser the context engine uses
(``litellm_token_counter`` via ``estimate_message_tokens``). It reports the
BEFORE (no proactive compaction) and AFTER (proactive compaction on) cumulative
totals; the numbers in the acceptance record come from RUNNING this, never an
estimate.

Baseline reference from the issue: ~11,060 cumulative input tokens, ~5,400 of it
re-transmitted evidence. AC-1 asks the AFTER total to drop measurably.
"""

from __future__ import annotations

from app.domain.llm import ChatMessage, Role, ToolCall
from app.llm.context import (
    ContextConfig,
    estimate_message_tokens,
    fit_transcript,
    litellm_token_counter,
    memoizing_token_counter,
)
from app.services.prompts.grounded_answer import GROUNDED_SYSTEM_PROMPT
from app.services.tools.impls.retrieval import rendered_snippet
from app.services.tools.registry import default_allowlist, tool_specs

# A FRONTIER model, pinned deliberately to exercise its tokeniser for the
# token-budget arithmetic below. NOT the registry default — since #490 the default
# is a FAST-tier model (claude-haiku-4.5); this constant is independent of it.
_MODEL = "openrouter/anthropic/claude-opus-4.8"

# A 3-tool answer's tools block: the real default read-only allow-list schemas
# (search_text / search_documents / list_documents / get_document …) — the same
# ~950-token stable prefix element the runtime advertises.
_TOOLS = tool_specs(default_allowlist())


def _rendered_search_result(doc: str, *, k: int = 6) -> str:
    """A realistic ``search_text`` reply: k passages, each an id-labelled snippet
    trimmed to the 600-char budget — mirrors ``retrieval._render_passages``."""
    body = (
        "In fiscal year 2024 the company reported material changes across every "
        "operating segment, and management discussed the drivers at length. "
    ) * 10
    blocks: list[str] = []
    for i in range(1, k + 1):
        chunk_id = f"{doc}-chunk-{i:04d}-9f2c1a"
        snippet = rendered_snippet(f"Passage {i} of {doc}. {body}")
        blocks.append(
            f"[{i}] {doc}.pdf (chunk {chunk_id}, chars {i * 600}-{i * 600 + 600}):\n{snippet}"
        )
    return "\n\n".join(blocks)


def _tool_call(call_id: str, query: str) -> ChatMessage:
    return ChatMessage(
        role=Role.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=call_id, name="search_text", arguments={"query": query}),),
    )


def _result(call_id: str, content: str) -> ChatMessage:
    return ChatMessage(role=Role.TOOL, content=content, tool_call_id=call_id, name="search_text")


# The three tool exchanges of the scripted answer (search → search → search →
# synthesize). Every retrieved passage becomes a citation at retrieval time (the
# runtime records them all, INV-3), so all three calls are "represented in
# citations" — exactly the proactive-compaction precondition.
_EXCHANGES = [
    ("call_1", "annual revenue by segment", _rendered_search_result("annual-report")),
    ("call_2", "operating margin drivers", _rendered_search_result("mdna")),
    ("call_3", "guidance for next fiscal year", _rendered_search_result("earnings-call")),
]
_CITED_SNIPPETS = {call_id: (content[:200],) for call_id, _q, content in _EXCHANGES}


def _cumulative_input_tokens(*, proactive: bool) -> int:
    """Sum the input tokens SENT across the 4 round-trips of a 3-tool answer.

    The runtime refits the grown transcript before every model turn; we mirror
    that (identity under budget when proactive is off; the budget-independent
    proactive pass when on) and count each round-trip's outgoing wire with the
    engine's own metric.
    """
    counter = memoizing_token_counter(litellm_token_counter(_MODEL))
    config = ContextConfig(
        # A frontier-sized window so the budget branch never trips — only the
        # proactive pass can shrink anything (the exact #491 condition).
        fallback_max_input_tokens=1_000_000,
        output_headroom_tokens=8_000,
        proactive_compaction_enabled=proactive,
    )
    messages: list[ChatMessage] = [
        ChatMessage(role=Role.SYSTEM, content=GROUNDED_SYSTEM_PROMPT),
        ChatMessage(role=Role.USER, content="Summarise the FY2024 results by segment."),
    ]

    def _refit() -> list[ChatMessage]:
        return fit_transcript(
            messages,
            model=_MODEL,
            tools=_TOOLS,
            config=config,
            counter=counter,
            max_input_resolver=lambda _m: 1_000_000,
            cited_snippets=_CITED_SNIPPETS,
        )

    total = 0
    # Round-trip 1: system + tools + question.
    messages = _refit()
    total += estimate_message_tokens(messages, _TOOLS, counter=counter)
    # Round-trips 2-4: each appends the previous turn's tool call + result, refits
    # (proactive shrinks the now-superseded older results), and re-sends.
    for call_id, query, content in _EXCHANGES:
        messages = [*messages, _tool_call(call_id, query), _result(call_id, content)]
        messages = _refit()
        total += estimate_message_tokens(messages, _TOOLS, counter=counter)
    return total


def test_ac1_cumulative_input_tokens_drop_with_proactive_compaction() -> None:
    before = _cumulative_input_tokens(proactive=False)
    after = _cumulative_input_tokens(proactive=True)

    # Recorded to the run log so the acceptance numbers come from execution.
    print(
        f"\n[#491 AC-1] cumulative input tokens: before={before} after={after} "
        f"saved={before - after} ({100 * (before - after) / before:.1f}%)"
    )

    # The baseline is in the ballpark the issue measured (~11,060) — a guard that
    # the harness assembles a realistic 3-tool answer, not that it hit an exact
    # figure (tokeniser drift is expected).
    assert 8_000 <= before <= 16_000
    # AC-1: the AFTER total drops measurably (the superseded evidence stops riding
    # every later turn at full size).
    assert after < before
    assert (before - after) / before >= 0.10
