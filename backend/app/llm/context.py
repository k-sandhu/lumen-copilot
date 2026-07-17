"""Token-budgeted context assembly (ADR-0016 §1, issue #410).

The one place the answer prompt is *built*. The runtime hands this module the
run's inputs — the composed system prompt, the persisted history, the new
question, and the advertised tool specs — and gets back the message list to
send **plus a `ContextBudget`** describing what was spent and the retrieval
knobs the run should use, assembled under an explicit token budget derived from
the model's input window.

The segments and their order are the ADR-0016 §1/§2 cache-first contract
(most-stable first): ``tools → system → memory → summary → history → live``.
``memory`` (ADR-0017) and ``summary`` (ADR-0016 §3.2) are **reserved** — their
content is empty in this feature; the slots exist so those later features drop
in without reordering the prefix and invalidating caches. Only ``history`` is
shrinkable in v1.

Why this exists (ADR-0016 §1): the previous approach was count-based (the last N
prior turns, whatever their size — a fixed slice that used to live in
``chat_service``) and cache-blind, and an oversize prompt failed *reactively* as the provider's
``ContextWindowExceeded`` → 422. Here the budget is enforced **before** the
call, with a deliberate degrade order: shrink the (reserved) memory segment →
roll history into the (reserved) summary → drop oldest history messages → a
**typed refusal** (a prompt whose fixed segments — system + tools + question +
headroom — cannot fit is a ``context_too_large`` 422, mapped by the runtime to
the terminal problem envelope, never a provider crash). The retrieval knobs
(``default_k``, snippet budget) are **derived here and passed into the run's
``ToolContext``** (ADR-0016 §1) so the assembler sets them *before* any tool
runs — it never "shrinks k retroactively".

Layering (ADR-0004): prompt assembly is part of the model boundary, so it lives
in ``llm/`` beside the gateway. LiteLLM is imported **lazily** inside the
default seams only (importing this module never pulls it), and both seams — the
token counter and the max-input resolver — are injectable, so the assembler
itself is pure and offline-testable, and a tokenizer/model-map failure degrades
to a heuristic, never a crashed answer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.llm import ChatMessage, Role, ToolSpec

log = get_logger(__name__)

#: Conservative input-window fallback when the model is unknown to the local
#: model map (AC-3): small enough to be safe on every model the platform routes
#: today, large enough not to regress ordinary conversations.
DEFAULT_FALLBACK_MAX_INPUT_TOKENS = 100_000
#: Room reserved for the model's *output* (the completion) inside the total
#: window; bounds how much of the window assembly may spend.
DEFAULT_OUTPUT_HEADROOM_TOKENS = 8_000
#: Fixed safety margin against tokenizer drift: the local estimate and the
#: provider's real tokenizer disagree by a few percent; the margin absorbs it.
_SAFETY_MARGIN_TOKENS = 1_024
#: The retrieval ``k`` an ordinary (roomy) budget uses — mirrors the runtime's
#: historical ``_DEFAULT_K`` so behaviour is unchanged when the window is ample.
_DEFAULT_RETRIEVAL_K = 6
#: The ``k`` a *tight* budget falls back to (degrade order step 3): fewer, so a
#: near-full window is not immediately blown by the next search's passages.
_TIGHT_RETRIEVAL_K = 3
#: The retrieval-``k`` ceiling under a ROOMY budget — the WIDEST any retrieval
#: tool allows (``list_documents``'s ``_LIST_MAX`` of 50). Each tool still clamps
#: to its OWN cap (``search_*`` to 20), so a roomy run is unchanged; a tight
#: budget lowers this so EVERY tool — including ``list_documents`` — shrinks
#: (ADR-0016 §1 degrade order, #424 final re-review).
_ROOMY_MAX_K = 50
#: Per-passage snippet char budget under a roomy window — mirrors
#: ``retrieval.impls._SNIPPET_BUDGET`` so a roomy run renders exactly as before.
_ROOMY_SNIPPET_BUDGET = 600
#: The tighter per-passage snippet budget when the window is nearly full, so each
#: returned passage adds less to the transcript (degrade order step 3).
_TIGHT_SNIPPET_BUDGET = 300
#: The remaining-history fraction below which the budget is considered "tight"
#: and the retrieval knobs shrink.
_TIGHT_BUDGET_FRACTION = 0.15
#: Wire framing the ``messages`` array adds beyond each entry's own JSON: a small
#: allowance per entry (the inter-entry separator) so the token estimate is a true
#: upper bound, not just a per-object sum (#424 third re-review).
_MESSAGE_FRAMING_TOKENS = 4
#: The outer ``[]`` framing of the messages array (counted once).
_ARRAY_FRAMING_TOKENS = 2
#: How many chars of a tool result's ACTUAL content the compaction digest keeps
#: (ADR-0016 §3.1 / #415): a bounded, content-bearing head extract — NOT the
#: ≤300-char user summary — so the model retains real evidence, just less of it.
DEFAULT_COMPACTION_DIGEST_CHARS = 1200
#: How many tool results one compaction pass clears at once (ADR-0016 §3.1): a
#: CHUNK, so cache invalidation is paid rarely (once per chunk) not per-turn.
DEFAULT_COMPACTION_CHUNK_SIZE = 4
#: The marker appended to a compacted tool result so the model (and a reader) knows
#: the content was truncated and that grounding lives in the recorded citations.
_COMPACTION_MARKER = (
    "\n[… earlier tool output truncated to fit the context window; "
    "the answer's recorded citations preserve the cited evidence …]"
)

#: Counts the tokens of a TEXT fragment for the answer's model.
TokenCounter = Callable[[str], int]
#: Resolves a model id to its max input tokens, or ``None`` when unknown.
MaxInputResolver = Callable[[str], int | None]


@dataclass(frozen=True, slots=True)
class ContextConfig:
    """The deploy-tunable assembly knobs (``Settings``-fed; defaults match)."""

    fallback_max_input_tokens: int = DEFAULT_FALLBACK_MAX_INPUT_TOKENS
    output_headroom_tokens: int = DEFAULT_OUTPUT_HEADROOM_TOKENS
    #: In-answer tool-result compaction (ADR-0016 §3.1 / #415): the digest size
    #: (chars of real content kept per compacted result) and the chunk size (how
    #: many results are cleared per pass, amortizing cache invalidation).
    compaction_digest_chars: int = DEFAULT_COMPACTION_DIGEST_CHARS
    compaction_chunk_size: int = DEFAULT_COMPACTION_CHUNK_SIZE


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """The accounting + derived retrieval knobs the assembler hands back (ADR-0016 §1).

    ``messages`` is the assembled prompt. ``input_budget_tokens`` is the window
    left for input after headroom + safety margin; ``estimated_tokens`` is what
    the assembled prompt is estimated to spend. ``dropped_history_messages`` is
    how many oldest turns the degrade order shed (AC-1).

    ``retrieval_k`` is the run's DEFAULT search ``k`` (used when the model omits
    it); ``max_k`` is the enforceable CEILING the runtime threads into the
    ``ToolContext`` so that even an explicit, model-supplied ``k`` is clamped to
    the budget (#424 review, finding 2/3); ``snippet_budget`` caps the per-passage
    text each search renders into the transcript. Under a tight budget all three
    shrink so a search issued *after* assembly cannot immediately overflow the
    window it was budgeted against (degrade order step 3).
    """

    messages: list[ChatMessage]
    input_budget_tokens: int
    estimated_tokens: int
    dropped_history_messages: int
    retrieval_k: int = _DEFAULT_RETRIEVAL_K
    max_k: int = _ROOMY_MAX_K
    snippet_budget: int = _ROOMY_SNIPPET_BUDGET


@dataclass(frozen=True, slots=True)
class _Segments:
    """The reserved semi-stable segments (ADR-0016 §1 rows 3–4).

    Empty in this feature — the slots exist so ADR-0017 memory and ADR-0016 §3.2
    summaries drop in between ``system`` and ``history`` without reordering the
    cache prefix. Modelled as message lists so a future filler is a pure change
    here.
    """

    memory: list[ChatMessage] = field(default_factory=list)
    summary: list[ChatMessage] = field(default_factory=list)


def litellm_token_counter(model: str) -> TokenCounter:
    """The default counter seam: LiteLLM's tokenizer for ``model``, lazily.

    A model whose tokenizer LiteLLM does not know (or a litellm import failure)
    falls back to :func:`_conservative_tokens` — a UTF-8 byte UPPER bound — so a
    counter never fails an answer *and* never under-counts into an overflow.
    """

    def _count(text: str) -> int:
        if not text:
            return 0
        try:
            # The import is inside the guard (#424 review, finding 5): if litellm
            # itself fails to import/initialise, we still degrade to the
            # conservative estimate rather than crashing the answer.
            import litellm  # lazy: no import cost until first assembly

            # litellm ships a py.typed marker but does not export token_counter
            # in its stubs — the ``litellm.*`` mypy override only silences missing
            # imports, not this attribute, so ignore it narrowly here.
            return int(litellm.token_counter(model=model, text=text))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — heuristic beats a crashed answer
            return _conservative_tokens(text)

    return _count


def _conservative_tokens(text: str) -> int:
    """A model-independent UPPER bound on the token count (#424 review, finding 4).

    The prior ``len // 4`` heuristic is an English-prose *average*, not a bound:
    CJK, emoji, code, and dense JSON can run ~1 token per character (or more per
    byte), so ``//4`` UNDER-counts and a tokenizer failure could still let an
    oversized prompt reach the provider — defeating pre-call enforcement. A
    subword (BPE/SentencePiece) token maps to at least one UTF-8 byte and usually
    several, so the **UTF-8 byte length is a safe upper bound** on the token
    count. It over-estimates for ASCII prose, but the fallback only fires when the
    real tokenizer is unavailable, where erring toward *trimming* beats erring
    toward *overflow*.
    """
    return max(1, len(text.encode("utf-8")))


def litellm_max_input_tokens(model: str) -> int | None:
    """The default resolver seam: LiteLLM's model map, lazily; ``None`` if unknown.

    Returns **only** a genuine input-window figure. A missing/invalid
    ``max_input_tokens`` is treated as *unknown* (``None`` → the caller uses its
    configured conservative fallback). We deliberately do NOT fall back to
    ``max_tokens`` (#424 review, finding 2): in pinned LiteLLM 1.55.9 that field
    is the *output* limit for some models (e.g. a model reporting only
    ``max_tokens=8192``), so using it as the input budget would floor the window
    and falsely refuse even a trivial prompt as ``context_too_large``.
    """
    try:
        import litellm  # lazy — inside the guard so an import failure degrades

        # Not in litellm's exported stubs — narrow ignore (see token_counter above).
        info = litellm.get_model_info(model=model)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — an unknown model uses the fallback budget
        return None
    raw = info.get("max_input_tokens")
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def assemble_context(
    *,
    model: str,
    system_prompt: str,
    history: Sequence[ChatMessage],
    question: str,
    tools: Sequence[ToolSpec] = (),
    config: ContextConfig | None = None,
    counter: TokenCounter | None = None,
    max_input_resolver: MaxInputResolver | None = None,
) -> ContextBudget:
    """Assemble the prompt under the model's input budget; return a :class:`ContextBudget`.

    Order: ``[system, *memory, *summary, *history, question]`` (memory/summary
    reserved-empty in v1). The degrade order (ADR-0016 §1): the reserved
    segments would shrink first (they are empty, so nothing to shed yet), then
    **history trims oldest-first** (whole messages, deterministic) until the
    estimate fits, then the retrieval knobs tighten; if the *fixed* segments
    (system + tool schemas + question + overheads) alone exceed the budget it
    refuses with a typed ``ValidationError`` (``context_too_large``, 422).

    Pure over its inputs + seams: no I/O, no globals — the default seams do the
    lazy LiteLLM work, and tests inject deterministic fakes.
    """
    cfg = config or ContextConfig()
    count = counter or litellm_token_counter(model)
    resolve = max_input_resolver or litellm_max_input_tokens
    segments = _Segments()  # reserved-empty; memory/summary land here later

    max_input = resolve(model) or cfg.fallback_max_input_tokens
    # A degenerate window (tiny model / oversized headroom) still yields a
    # positive floor so the question alone can be judged against SOMETHING.
    budget = max(max_input - cfg.output_headroom_tokens - _SAFETY_MARGIN_TOKENS, 1)

    # The fixed spend: system + the reserved segments (empty now) + question +
    # the serialized tool schemas — each counted in its exact WIRE form (the same
    # metric :func:`estimate_message_tokens` / :func:`fit_transcript` use, so the
    # initial assembly and the per-turn refit never disagree about a message's
    # size, #424 re-review). Tools ride the ``tools`` param but spend the window.
    reserved = [*segments.memory, *segments.summary]
    system_message = ChatMessage(role=Role.SYSTEM, content=system_prompt)
    question_message = ChatMessage(role=Role.USER, content=question)
    tools_text = _tools_wire_text(tools)

    def _msg_cost(m: ChatMessage) -> int:
        # A message's wire form + the per-entry array framing — the SAME per-message
        # cost :func:`estimate_message_tokens` / :func:`fit_transcript` use, so the
        # initial assembly and the per-turn refit agree exactly (#424 third re-review).
        return count(_message_wire_text(m)) + _MESSAGE_FRAMING_TOKENS

    fixed = (
        _msg_cost(system_message)
        + _msg_cost(question_message)
        + count(tools_text)
        + _ARRAY_FRAMING_TOKENS
        + sum(_msg_cost(m) for m in reserved)
    )
    if fixed > budget:
        raise ValidationError(
            "The question, instructions, and tools exceed the model's context "
            "window. Shorten the question or choose a larger-context model.",
            code="context_too_large",
        )

    # History: newest-first accumulation under the remaining budget, then
    # restored to chronological order — so the freshest turns always survive and
    # the drop point is deterministic for identical inputs (AC-2).
    remaining = budget - fixed
    kept_reversed: list[ChatMessage] = []
    dropped = 0
    spent = 0
    for message in reversed(list(history)):
        message_tokens = _msg_cost(message)
        if spent + message_tokens > remaining:
            # Everything older than the first non-fitting message drops too — a
            # gap in the middle of a conversation reads worse than an
            # oldest-first cut.
            dropped = len(history) - len(kept_reversed)
            break
        kept_reversed.append(message)
        spent += message_tokens

    kept = list(reversed(kept_reversed))
    if dropped:
        log.info(
            "context.history_trimmed",
            dropped_messages=dropped,
            kept_messages=len(kept),
            budget_tokens=budget,
        )

    messages: list[ChatMessage] = [system_message, *reserved, *kept, question_message]
    # Degrade order step 3: under a tight remaining window, tell the run to
    # retrieve fewer passages so the next search does not immediately overflow —
    # both the default ``k`` AND the enforceable ceiling shrink, so even an
    # explicit large ``k`` is clamped (#424 review, finding 3).
    tight = (remaining - spent) < _TIGHT_BUDGET_FRACTION * budget
    retrieval_k = _TIGHT_RETRIEVAL_K if tight else _DEFAULT_RETRIEVAL_K
    max_k = _TIGHT_RETRIEVAL_K if tight else _ROOMY_MAX_K
    snippet_budget = _TIGHT_SNIPPET_BUDGET if tight else _ROOMY_SNIPPET_BUDGET

    return ContextBudget(
        messages=messages,
        input_budget_tokens=budget,
        estimated_tokens=fixed + spent,
        dropped_history_messages=dropped,
        retrieval_k=retrieval_k,
        max_k=max_k,
        snippet_budget=snippet_budget,
    )


def estimate_message_tokens(
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolSpec] = (),
    *,
    counter: TokenCounter,
) -> int:
    """Estimate the tokens an outgoing ``messages`` + ``tools`` payload will spend.

    Counts the FULL wire form of each message — not just ``content`` — because the
    gateway also serializes ``tool_calls`` (name + arguments JSON + id),
    ``tool_call_id``, and ``name`` (#424 re-review): a model that emits a large
    tool *argument* would otherwise be radically under-counted (a probe measured
    estimate 18 vs actual wire 5231) and slip past the guard. Plus the serialized
    tool schemas, plus the ``messages`` array framing (outer brackets + the
    separators between entries, which no single entry's JSON includes) so the
    total is a true UPPER bound, not a per-object sum (#424 third re-review).
    Exposed so the runtime can re-check the *grown* transcript before every model
    call (#424 review, finding 1).
    """
    return (
        counter(_tools_wire_text(tools))
        + _ARRAY_FRAMING_TOKENS
        + sum(counter(_message_wire_text(m)) + _MESSAGE_FRAMING_TOKENS for m in messages)
    )


def _message_wire_text(message: ChatMessage) -> str:
    """Serialize one message to the EXACT wire shape the gateway sends, for counting.

    Mirrors :meth:`LLMGateway._to_wire_messages` field-for-field (role, content,
    the OpenAI ``tool_calls`` array with each call's id/name/serialized arguments,
    ``tool_call_id``, and ``name``) so the estimate counts every byte the provider
    actually receives — tool-call metadata included (#424 re-review, new finding
    1). ``sort_keys`` for determinism.
    """
    import json

    entry: dict[str, object] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        entry["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in message.tool_calls
        ]
    if message.tool_call_id is not None:
        entry["tool_call_id"] = message.tool_call_id
    if message.name is not None:
        entry["name"] = message.name
    try:
        return json.dumps(entry, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover — args are JSON by construction
        return str(entry)


def fit_transcript(
    messages: Sequence[ChatMessage],
    *,
    model: str,
    tools: Sequence[ToolSpec] = (),
    config: ContextConfig | None = None,
    counter: TokenCounter | None = None,
    max_input_resolver: MaxInputResolver | None = None,
    protected_tool_call_ids: frozenset[str] = frozenset(),
) -> list[ChatMessage]:
    """Re-fit an already-grown transcript to the model's input budget (#424, finding 1).

    :func:`assemble_context` only budgets the FIRST model call. The agentic loop
    then appends each turn's assistant tool-call message and (uncounted) tool
    results and re-sends the grown list on the next turn and the forced-synthesis
    call — so a long result or many turns could still overflow the provider, the
    very failure this module exists to prevent. The runtime therefore calls this
    before **every** ``stream_tools`` turn.

    The degrade order, applied only when over budget:

    1. **Shed conversation history.** The head (the system message at index 0) and
       the current answer's live tail (the question + this answer's tool-call /
       result messages, kept paired) are protected; only the plain prior turns
       between them are shed, oldest-first, in whole units.
    2. **Compact the live tail's oldest tool results** (ADR-0016 §3.1 / #415).
       Rather than refuse, replace the OLDEST tool results' contents with a
       bounded, content-bearing digest (:func:`_context_digest`), cleared in
       CHUNKS so cache invalidation is amortized. A result whose passages the
       answer **already cited** (its ``tool_call_id`` in ``protected_tool_call_ids``)
       is kept verbatim, so grounding for existing citations survives (INV-3 also
       holds structurally: citations are recorded at retrieval time, independent
       of the transcript).
    3. **Refuse.** Only if the system + question + protected/compacted tail still
       cannot fit does it raise the typed ``context_too_large`` — a deterministic
       refusal, never an over-budget call.
    """
    cfg = config or ContextConfig()
    count = counter or litellm_token_counter(model)
    resolve = max_input_resolver or litellm_max_input_tokens
    budget = max(
        (resolve(model) or cfg.fallback_max_input_tokens)
        - cfg.output_headroom_tokens
        - _SAFETY_MARGIN_TOKENS,
        1,
    )

    working = list(messages)
    # Cost each message ONCE (its wire form + the per-entry array framing), then
    # shed by subtracting from a running total — linear, not the O(n²)
    # re-count-per-deletion the naive loop would be (#424 re-review, perf).
    # ``tools`` + the array brackets are costed once; invariant across shedding.
    costs = [count(_message_wire_text(m)) + _MESSAGE_FRAMING_TOKENS for m in working]
    fixed_cost = count(_tools_wire_text(tools)) + _ARRAY_FRAMING_TOKENS
    total = fixed_cost + sum(costs)
    if total <= budget:
        return working

    # The protected tail begins at the LAST plain user message (the current
    # question); everything from there on is this answer's live turn (question +
    # this answer's tool-call/result messages). Only the *conversation history*
    # between the system head and that question is shed.
    tail_start = _last_question_index(working)
    dropped = 0
    # Shed the oldest history UNIT atomically: either a single plain message, or a
    # complete assistant(tool_calls) + its following tool-result messages. Dropping
    # whole groups means a tool result is never orphaned from its call (#424 third
    # re-review) AND a historical tool group can still be shed when doing so would
    # fit (#424 final re-review) — not skipped into an avoidable refusal. If the
    # history region is empty, stop and let the refusal below fire.
    while total > budget:
        span = _oldest_sheddable_span(working, tail_start)
        if span is None:
            break
        start, end = span
        total -= sum(costs[start:end])
        del working[start:end]
        del costs[start:end]
        tail_start -= end - start
        dropped += end - start

    if dropped:
        log.info("context.transcript_refit", dropped_messages=dropped, budget_tokens=budget)

    # (2) Compact the live tail's oldest tool results (ADR-0016 §3.1 / #415) —
    # digest them rather than refuse. Only reached if shedding history was not
    # enough. Chunked so cache invalidation is paid rarely; cited results are
    # protected. Mutates ``working``/``costs``/``total`` in place.
    if total > budget:
        total, compacted = _compact_oldest_tool_results(
            working,
            costs,
            tail_start=tail_start,
            budget=budget,
            total=total,
            count=count,
            digest_chars=cfg.compaction_digest_chars,
            chunk_size=max(1, cfg.compaction_chunk_size),
            protected=protected_tool_call_ids,
        )
        if compacted:
            log.info(
                "context.tool_results_compacted",
                compacted=compacted,
                budget_tokens=budget,
            )

    if total > budget:
        # (3) Even with history shed AND old tool results compacted, the system
        # prompt + question + protected/recent tail exceed the window. Refuse
        # deterministically rather than send an over-budget call.
        raise ValidationError(
            "This answer's context exceeded the model's window even after "
            "trimming history. Start a new conversation or choose a "
            "larger-context model.",
            code="context_too_large",
        )
    return working


def _last_question_index(messages: Sequence[ChatMessage]) -> int:
    """Index of the current question — the last plain user message (no tool_call_id).

    The runtime assembles ``[system, *history, question]`` and then only ever
    appends this answer's assistant(tool_calls) + tool messages, so the current
    question is the last ``role == user`` message that is not a tool result. Its
    index marks where the protected live tail begins. Falls back to ``len`` (all
    protected) if — defensively — no user message is present.
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role is Role.USER and m.tool_call_id is None:
            return i
    return len(messages)


def _oldest_sheddable_span(
    messages: Sequence[ChatMessage], tail_start: int
) -> tuple[int, int] | None:
    """The ``[start, end)`` of the oldest history UNIT safe to shed, or ``None``.

    Considers only the history region ``[1, tail_start)`` (after the system head,
    before the protected question + live tool tail). A "unit" is:

    * a **plain** message (no ``tool_calls``, no ``tool_call_id``) → span of one; or
    * an assistant **tool-call** message plus the contiguous ``tool``-result
      messages that follow it → the whole group, dropped atomically so a result is
      never orphaned from its call (#424 third re-review) and a historical tool
      group can still be shed when that would fit (#424 final re-review).

    A dangling ``tool`` result with no preceding call (defensive — the runtime never
    produces this in history) is treated as its own one-message unit. Returns the
    OLDEST such unit; ``None`` when the region is empty. The runtime's persisted
    history is text-only, so in practice every unit is a single plain message.
    """
    if tail_start <= 1:
        return None
    start = 1
    m = messages[start]
    if m.tool_calls:
        # An assistant tool-call: extend over the contiguous tool results it owns.
        end = start + 1
        while end < tail_start and messages[end].tool_call_id is not None:
            end += 1
        return (start, end)
    return (start, start + 1)


def _context_digest(content: str, digest_chars: int) -> str:
    """A bounded, content-bearing digest of a tool result's content (ADR-0016 §3.1).

    Keeps the first ``digest_chars`` of the ACTUAL content — for a rendered
    retrieval result that is the first labelled passages (document + chunk id +
    the start of each snippet), so the model retains real, attributable evidence,
    just less of it — then appends a marker noting the truncation. This is
    deliberately NOT the ≤300-char user-facing ``ToolResult.summary`` (a count
    like "3 passages" is not evidence). Content already within budget is returned
    unchanged (nothing to gain by digesting it).
    """
    trimmed = content[: max(1, digest_chars)].rstrip()
    if len(trimmed) >= len(content.rstrip()):
        return content
    return trimmed + _COMPACTION_MARKER


def _compact_oldest_tool_results(
    working: list[ChatMessage],
    costs: list[int],
    *,
    tail_start: int,
    budget: int,
    total: int,
    count: TokenCounter,
    digest_chars: int,
    chunk_size: int,
    protected: frozenset[str],
) -> tuple[int, int]:
    """Digest the OLDEST live-tail tool results in chunks until it fits (#415).

    Operates only on the live tail ``[tail_start, len)`` — this answer's tool
    activity — never on history (already shed) or the system/question. A candidate
    is a ``tool``-result message whose content is longer than the digest (already-
    short ones gain nothing).

    Two tiers, so cited evidence is preferentially preserved: **uncited** results
    (``tool_call_id`` not in ``protected``) are compacted first; only if the budget
    STILL cannot be met are the ``protected`` (cited) results compacted too — a
    degraded-but-answered turn beats a refusal. Within each tier, oldest-first in
    **chunks** of ``chunk_size`` (even when fewer would suffice) so the prefix
    changes once per chunk, not per turn (amortized cache invalidation). Mutates
    ``working``/``costs`` in place; returns ``(new_total, compacted_count)``.
    """
    compacted = 0
    for include_cited in (False, True):
        if total <= budget:
            break
        # Tier 1 compacts uncited results; tier 2 then re-scans and — since the
        # just-compacted uncited results are now short (excluded by the length
        # filter) — targets the remaining cited ones.
        candidates = [
            i
            for i in range(tail_start, len(working))
            if working[i].role is Role.TOOL
            and len(working[i].content) > digest_chars
            and (include_cited or working[i].tool_call_id not in protected)
        ]
        pos = 0
        while total > budget and pos < len(candidates):
            for i in candidates[pos : pos + chunk_size]:
                digested = replace(
                    working[i], content=_context_digest(working[i].content, digest_chars)
                )
                new_cost = count(_message_wire_text(digested)) + _MESSAGE_FRAMING_TOKENS
                total += new_cost - costs[i]
                working[i] = digested
                costs[i] = new_cost
                compacted += 1
            pos += chunk_size
    return total, compacted


def _tools_wire_text(tools: Sequence[ToolSpec]) -> str:
    """Serialize the tools to the EXACT wire shape the gateway sends, for counting.

    Earlier this counted only ``name + description + parameters`` and dropped the
    OpenAI/LiteLLM ``{"type":"function","function":{…}}`` wrapper, its keys,
    separators, and the list framing that :meth:`LLMGateway._to_wire_tools`
    actually builds — so a large tool allow-list could spend far more of the
    window than the estimate showed, letting a tools-only oversized prompt slip
    past the guard (#424 review, finding 3). This mirrors that wire dict exactly
    (``sort_keys`` for determinism), so every byte of tool framing is counted.
    """
    import json

    payload = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]
    try:
        return json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover — schemas are JSON by construction
        return str(payload)


__all__ = [
    "DEFAULT_COMPACTION_CHUNK_SIZE",
    "DEFAULT_COMPACTION_DIGEST_CHARS",
    "DEFAULT_FALLBACK_MAX_INPUT_TOKENS",
    "DEFAULT_OUTPUT_HEADROOM_TOKENS",
    "ContextBudget",
    "ContextConfig",
    "MaxInputResolver",
    "TokenCounter",
    "assemble_context",
    "estimate_message_tokens",
    "fit_transcript",
    "litellm_max_input_tokens",
    "litellm_token_counter",
]
