"""Deterministic prompt/cache economics (#764, superseding #658's raw ceiling).

Four calls of the historical #491 three-search workload, counted with the
context engine tokenizer. Every complete retrieved passage is citation-backed,
as in the runtime; no 200-character evidence substitute. Prices are normalized
input-token units, NOT provider invoices: cached reads 0.1x, writes/uncached
spans 1.25x. The model caches complete identical message prefixes; a rewrite
invalidates the changed message and everything after it (no suffix reuse).
"""

from __future__ import annotations

from dataclasses import dataclass

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
from app.services.tools.registry import tool_specs

_MODEL = "openrouter/anthropic/claude-opus-4.8"
_LEGACY_BENCHMARK_TOOLS = frozenset(
    {"search_text", "search_documents", "list_documents", "get_document", "ask_user"}
)
_TOOLS = tool_specs(_LEGACY_BENCHMARK_TOOLS)
_READ_MULTIPLIER = 0.1
_WRITE_MULTIPLIER = 1.25


def _passages(doc: str, *, k: int = 6) -> tuple[str, ...]:
    body = (
        "In fiscal year 2024 the company reported material changes across every "
        "operating segment, and management discussed the drivers at length. "
    ) * 10
    return tuple(rendered_snippet(f"Passage {i} of {doc}. {body}") for i in range(1, k + 1))


def _rendered_passage_blocks(doc: str, *, k: int = 6) -> tuple[str, ...]:
    return tuple(
        f"[{i}] {doc}.pdf (chunk {doc}-chunk-{i:04d}-9f2c1a, "
        f"chars {i * 600}-{i * 600 + 600}):\n{snippet}"
        for i, snippet in enumerate(_passages(doc, k=k), start=1)
    )


def _rendered_search_result(doc: str, *, k: int = 6) -> str:
    return "\n\n".join(_rendered_passage_blocks(doc, k=k))


def _tool_call(call_id: str, query: str) -> ChatMessage:
    return ChatMessage(
        role=Role.ASSISTANT,
        content="",
        tool_calls=(ToolCall(id=call_id, name="search_text", arguments={"query": query}),),
    )


def _result(call_id: str, content: str) -> ChatMessage:
    return ChatMessage(role=Role.TOOL, content=content, tool_call_id=call_id, name="search_text")


_SEARCHES = [
    ("call_1", "annual revenue by segment", "annual-report"),
    ("call_2", "operating margin drivers", "mdna"),
    ("call_3", "guidance for next fiscal year", "earnings-call"),
]
_EXCHANGES = [(call, query, _rendered_search_result(doc)) for call, query, doc in _SEARCHES]
# Runtime _evidence protects the full labeled block, not just its passage body.
_CITED_SNIPPETS = {call: _rendered_passage_blocks(doc) for call, _query, doc in _SEARCHES}


@dataclass(frozen=True)
class Economics:
    raw_tokens: int
    cached_tokens: int
    written_tokens: int
    priced_tokens: float


def _price_prompts(prompts: list[list[ChatMessage]]) -> Economics:
    counter = memoizing_token_counter(litellm_token_counter(_MODEL))
    raw = cached = 0
    previous: list[ChatMessage] = []
    for current in prompts:
        tokens = estimate_message_tokens(current, _TOOLS, counter=counter)
        common = 0
        for old, new in zip(previous, current, strict=False):
            if old != new:
                break
            common += 1
        # The tool schemas are identical on every call, including when the first
        # message changes. First call is cold; later calls reuse schemas + whole
        # stable messages, never a suffix past a rewritten result.
        reuse = (
            estimate_message_tokens(current[:common], _TOOLS, counter=counter) if previous else 0
        )
        raw += tokens
        cached += reuse
        previous = current
    written = raw - cached
    return Economics(raw, cached, written, cached * _READ_MULTIPLIER + written * _WRITE_MULTIPLIER)


def _prompts(*, proactive: bool) -> list[list[ChatMessage]]:
    counter = memoizing_token_counter(litellm_token_counter(_MODEL))
    config = ContextConfig(
        fallback_max_input_tokens=1_000_000,
        output_headroom_tokens=8_000,
        proactive_compaction_enabled=proactive,
    )
    messages = [
        ChatMessage(role=Role.SYSTEM, content=GROUNDED_SYSTEM_PROMPT),
        ChatMessage(role=Role.USER, content="Summarise the FY2024 results by segment."),
    ]
    prompts = [list(messages)]
    for call_id, query, content in _EXCHANGES:
        messages = fit_transcript(
            [*messages, _tool_call(call_id, query), _result(call_id, content)],
            model=_MODEL,
            tools=_TOOLS,
            config=config,
            counter=counter,
            max_input_resolver=lambda _m: 1_000_000,
            cited_snippets=_CITED_SNIPPETS,
        )
        prompts.append(list(messages))
    return prompts


def test_complete_cited_passage_economics() -> None:
    default_prompts = _prompts(proactive=False)
    opt_in_prompts = _prompts(proactive=True)
    default = _price_prompts(default_prompts)
    opt_in = _price_prompts(opt_in_prompts)
    print(
        f"\n[#764 / #658] default_off={default} proactive_on={opt_in} "
        f"raw_delta={default.raw_tokens - opt_in.raw_tokens} "
        f"priced_savings={opt_in.priced_tokens - default.priced_tokens:.2f}"
    )
    # Retain the historical realistic-workload ceiling. Complete evidence is the
    # ground truth even if keeping it erases the old >=10% shrink assertion.
    assert 8_000 <= default.raw_tokens <= 20_000
    for prompts in (default_prompts, opt_in_prompts):
        for call_id, snippets in _CITED_SNIPPETS.items():
            for prompt in prompts:
                for message in prompt:
                    if message.tool_call_id == call_id:
                        assert all(snippet in message.content for snippet in snippets)
    assert opt_in.raw_tokens <= default.raw_tokens
    assert default.priced_tokens <= opt_in.priced_tokens
    assert default.cached_tokens >= opt_in.cached_tokens


def test_rewriting_an_early_result_prices_the_entire_suffix_as_uncached() -> None:
    from dataclasses import replace

    prompts = _prompts(proactive=False)
    original = prompts[-1]
    # Remove only an uncited wrapper, never a passage or its source label.
    # Every complete cited block remains verbatim in BOTH priced transcripts.
    annotated = [
        replace(message, content="Diagnostic source metadata. " * 8 + message.content)
        if message.tool_call_id == "call_1"
        else message
        for message in original
    ]
    stable = _price_prompts([annotated, annotated])
    churned = _price_prompts([annotated, original])
    for transcript in (annotated, original):
        for message in transcript:
            if message.tool_call_id is not None and message.tool_call_id in _CITED_SNIPPETS:
                assert all(
                    block in message.content for block in _CITED_SNIPPETS[message.tool_call_id]
                )
    print(
        f"\n[#764 rewrite probe] stable={stable} rewritten={churned} "
        f"priced_penalty={churned.priced_tokens - stable.priced_tokens:.2f}"
    )
    assert churned.raw_tokens < stable.raw_tokens
    assert churned.cached_tokens < stable.cached_tokens
    assert churned.written_tokens > stable.written_tokens
    assert churned.priced_tokens > stable.priced_tokens
