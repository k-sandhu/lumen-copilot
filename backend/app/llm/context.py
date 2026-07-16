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

Why this exists (ADR-0016 §1): the previous inline assembly was count-based
(the last N turns, whatever their size — ``chat_service._HISTORY_TURNS``) and
cache-blind, and an oversize prompt failed *reactively* as the provider's
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
from dataclasses import dataclass, field

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
#: Per-message wire overhead (role/framing tokens) added on top of content.
_PER_MESSAGE_OVERHEAD_TOKENS = 4
#: The retrieval ``k`` an ordinary (roomy) budget uses — mirrors the runtime's
#: historical ``_DEFAULT_K`` so behaviour is unchanged when the window is ample.
_DEFAULT_RETRIEVAL_K = 6
#: The ``k`` a *tight* budget falls back to (degrade order step 3): fewer, so a
#: near-full window is not immediately blown by the next search's passages.
_TIGHT_RETRIEVAL_K = 3
#: The remaining-history fraction below which the budget is considered "tight"
#: and the retrieval knobs shrink.
_TIGHT_BUDGET_FRACTION = 0.15

#: Counts the tokens of a TEXT fragment for the answer's model.
TokenCounter = Callable[[str], int]
#: Resolves a model id to its max input tokens, or ``None`` when unknown.
MaxInputResolver = Callable[[str], int | None]


@dataclass(frozen=True, slots=True)
class ContextConfig:
    """The deploy-tunable assembly knobs (``Settings``-fed; defaults match)."""

    fallback_max_input_tokens: int = DEFAULT_FALLBACK_MAX_INPUT_TOKENS
    output_headroom_tokens: int = DEFAULT_OUTPUT_HEADROOM_TOKENS


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """The accounting + derived retrieval knobs the assembler hands back (ADR-0016 §1).

    ``messages`` is the assembled prompt. ``input_budget_tokens`` is the window
    left for input after headroom + safety margin; ``estimated_tokens`` is what
    the assembled prompt is estimated to spend. ``dropped_history_messages`` is
    how many oldest turns the degrade order shed (AC-1). ``retrieval_k`` and
    ``snippet_budget`` are the knobs the runtime threads into the run's
    ``ToolContext`` so a search issued *after* assembly respects the same window
    (they shrink under a tight budget — degrade order step 3).
    """

    messages: list[ChatMessage]
    input_budget_tokens: int
    estimated_tokens: int
    dropped_history_messages: int
    retrieval_k: int = _DEFAULT_RETRIEVAL_K
    snippet_budget: int | None = None


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

    A model whose tokenizer LiteLLM does not know falls back to the classic
    ~4-chars-per-token heuristic — a counter must *never* fail an answer.
    """

    def _count(text: str) -> int:
        if not text:
            return 0
        import litellm  # lazy: no import cost until first assembly

        try:
            # litellm ships a py.typed marker but does not export token_counter
            # in its stubs — the ``litellm.*`` mypy override only silences missing
            # imports, not this attribute, so ignore it narrowly here.
            return int(litellm.token_counter(model=model, text=text))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — heuristic beats a crashed answer
            return max(1, len(text) // 4)

    return _count


def litellm_max_input_tokens(model: str) -> int | None:
    """The default resolver seam: LiteLLM's model map, lazily; ``None`` if unknown."""
    import litellm  # lazy

    try:
        # Not in litellm's exported stubs — narrow ignore (see token_counter above).
        info = litellm.get_model_info(model=model)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — an unknown model uses the fallback budget
        return None
    raw = info.get("max_input_tokens") or info.get("max_tokens")
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    return value or None


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

    # The fixed spend: system + the reserved segments (empty now) + question
    # (+ their per-message overheads) + the serialized tool schemas (they ride
    # the ``tools`` param but spend the same window).
    reserved = [*segments.memory, *segments.summary]
    tools_text = "".join(f"{t.name}{t.description}{_schema_text(t)}" for t in tools)
    fixed = (
        count(system_prompt)
        + count(question)
        + count(tools_text)
        + sum(count(m.content) + _PER_MESSAGE_OVERHEAD_TOKENS for m in reserved)
        + 2 * _PER_MESSAGE_OVERHEAD_TOKENS
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
        message_tokens = count(message.content) + _PER_MESSAGE_OVERHEAD_TOKENS
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

    messages: list[ChatMessage] = [
        ChatMessage(role=Role.SYSTEM, content=system_prompt),
        *reserved,
        *kept,
        ChatMessage(role=Role.USER, content=question),
    ]
    # Degrade order step 3: under a tight remaining window, tell the run to
    # retrieve fewer passages so the next search does not immediately overflow.
    tight = (remaining - spent) < _TIGHT_BUDGET_FRACTION * budget
    retrieval_k = _TIGHT_RETRIEVAL_K if tight else _DEFAULT_RETRIEVAL_K

    return ContextBudget(
        messages=messages,
        input_budget_tokens=budget,
        estimated_tokens=fixed + spent,
        dropped_history_messages=dropped,
        retrieval_k=retrieval_k,
    )


def _schema_text(tool: ToolSpec) -> str:
    """A stable text rendering of a tool's parameter schema for counting."""
    import json

    try:
        return json.dumps(tool.parameters, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover — schemas are JSON by construction
        return str(tool.parameters)


__all__ = [
    "DEFAULT_FALLBACK_MAX_INPUT_TOKENS",
    "DEFAULT_OUTPUT_HEADROOM_TOKENS",
    "ContextBudget",
    "ContextConfig",
    "MaxInputResolver",
    "TokenCounter",
    "assemble_context",
    "litellm_max_input_tokens",
    "litellm_token_counter",
]
