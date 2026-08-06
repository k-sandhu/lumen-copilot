"""The ``read_conversation`` recall tool (#569, epic #533 R1).

The original chat **is** preserved — ``messages`` is append-only and the user can
read every turn through ``GET /chat/sessions/{id}/messages``. The *model* cannot.
Once the rolling summariser folds a turn away the runtime assembles
``[summary] + [turns newer than the coverage cursor]``
(``ChatService.send_message``), so at today's defaults the model sees roughly the
last two turns verbatim plus a ≤300-word summary. "As we discussed earlier" then
resolves to a paraphrase at best, and to nothing once the detail falls out of the
summary. This tool is the missing function: a governed read back into the raw
transcript, taken **only when the model chooses to**.

Scoped to the **compacted range of the current session**. Turns still in the live
window are deliberately unreachable — they are already in the prompt verbatim, so
returning them would spend context to say what the context already says.

Like the retrieval tools this handler is a **thin adapter**: the
:class:`~app.services.tools.types.TranscriptReader` seam owns reaching the
messages table (ownership predicate, compacted-range bound, call budget); this
module owns the permission re-check on recalled evidence and the rendering.

**T0, read-only, no approval.** It reads one conversation the caller owns and
nothing else. The runner still audits it (``tool.invoked``/``tool.result``) and
records a ``tool_invocations`` row like every other call.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.domain.tools import ERROR_BAD_ARGS, ERROR_TOOL_ERROR, RiskTier, ToolHandlerResult
from app.services.tools.types import RecalledTurn, ToolContext, ToolDefinition

READ_CONVERSATION_TOOL_NAME = "read_conversation"

#: How many turns one call may return. Small on purpose: recall competes for the
#: same window the answer needs, and the useful recall is a handful of turns
#: around one remembered point, not a re-read of the conversation.
_MAX_TURNS = 10
_DEFAULT_TURNS = 6
#: Chars of each recalled turn the model sees. A recalled turn is prose the model
#: itself wrote (or the user typed), not evidence — so it gets a passage-sized
#: allowance, and the assembler lowers it further under a tight window.
_TURN_BUDGET = 600
#: Terms in one query. More than this is not a sharper search, it is a way to
#: build an expensive predicate.
_MAX_TERMS = 6
#: Longest single term. Bounds the LIKE pattern.
_MAX_TERM_CHARS = 64

#: What replaces an assistant turn whose evidence the caller may no longer read.
#: Withheld WHOLE, not trimmed: the leak is that the turn quotes the document
#: inline, and there is no reliable way to excise a quotation from prose — the
#: revoked passage could be anywhere in it, paraphrased, or split across
#: sentences. Naming the turn without its text keeps recall honest (the model
#: learns a turn exists and can ask the user) while disclosing nothing.
_WITHHELD = (
    "[withheld — this turn quotes a document you can no longer access. "
    "Ask the user about it rather than guessing at its contents.]"
)


def _parse_terms(raw: object) -> tuple[list[str], str | None]:
    """Split the model's query into bounded search terms, or explain the refusal.

    Returns ``(terms, error)``. A blank/absent query is not an error — it is the
    "what came just before what I can see" read, the most common recall there is.
    """
    if raw is None:
        return [], None
    if not isinstance(raw, str):
        return [], "query must be a string."
    terms = [t for t in raw.split() if t]
    if len(terms) > _MAX_TERMS:
        return [], f"query has too many terms (max {_MAX_TERMS}); search for fewer, sharper words."
    for term in terms:
        if len(term) > _MAX_TERM_CHARS:
            return [], f"a search term is too long (max {_MAX_TERM_CHARS} characters)."
    return terms, None


def _clamp_turns(value: object, maximum: int) -> int:
    if not isinstance(value, int | float | str):
        return max(1, min(maximum, _DEFAULT_TURNS))
    try:
        k = int(value)
    except (TypeError, ValueError):
        return max(1, min(maximum, _DEFAULT_TURNS))
    return max(1, min(maximum, k))


def _render_turn(turn: RecalledTurn, *, text: str, budget: int) -> str:
    body = text.strip()
    if len(body) > budget:
        body = body[:budget].rstrip() + "…"
    stamp = turn.created_at.isoformat(timespec="seconds")
    return f"[{stamp}] {turn.role}:\n{body}"


async def _read_conversation(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    """Read back the compacted range of this conversation, permission-re-checked."""
    if ctx.transcript is None:
        # No conversation to recall (a sub-agent, a headless run). Typed refusal,
        # never a crash — the run continues, exactly as ``run_python`` does
        # without its sandbox seam.
        return ToolHandlerResult(
            content=(
                "read_conversation is unavailable here: this run has no conversation "
                "history to read. Answer from what you were given."
            ),
            ok=False,
            error=ERROR_TOOL_ERROR,
            summary="no transcript in this context",
        )

    terms, bad = _parse_terms(args.get("query"))
    if bad is not None:
        return ToolHandlerResult(
            content=f"read_conversation rejected: {bad}",
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="invalid query",
        )
    # Clamp against the budget-derived ceiling as well as this tool's own max
    # (the ADR-0016 §1 rule every retrieval tool follows): a tight window lowers
    # ``ctx.max_k``, and a recall issued after assembly must not overflow the
    # context it was budgeted against.
    limit = _clamp_turns(args.get("k"), maximum=min(_MAX_TURNS, ctx.max_k))

    outcome = await ctx.transcript.recall(query=" ".join(terms) or None, limit=limit)

    if outcome.refusal is not None:
        # The stopping rule fired. ``ok=False`` so the model reads it as a wall
        # rather than as a thin result worth retrying with different words.
        return ToolHandlerResult(
            content=outcome.refusal,
            ok=False,
            error=ERROR_TOOL_ERROR,
            summary="recall budget spent",
        )
    if not outcome.compaction_started:
        return ToolHandlerResult(
            content=(
                "Nothing has been compacted yet — this whole conversation is already "
                "in your context above. Read it there; do not call this again for "
                "this conversation."
            ),
            summary="nothing compacted",
        )
    if not outcome.turns:
        return ToolHandlerResult(
            content=(
                "No earlier turn matches that. Try one or two different keywords, or "
                "ask the user what they are referring to."
                if terms
                else "There are no earlier turns beyond what you can already see."
            ),
            summary="0 turns",
        )

    # --- the permission re-check ------------------------------------------------
    # A recalled assistant turn quotes documents inline, so replaying it verbatim
    # would re-serve text whose grant may since have been revoked (#533 design
    # rule 1; the #536 class of leak from a third direction). ONE bounded,
    # permission-filtered metadata query — the same ``retrieval/`` chokepoint the
    # evidence carry-forward uses, so it inherits connector-ACL mode and the
    # ADR-0022 group principals — decides what survives.
    cited_ids = {doc_id for turn in outcome.turns for doc_id in turn.cited_document_ids}
    permitted: dict[UUID, str] = {}
    if cited_ids:
        permitted = await ctx.retrieval.permitted_document_names(
            principal=ctx.principal, document_ids=sorted(cited_ids, key=str)
        )

    budget = max(1, min(ctx.snippet_budget, _TURN_BUDGET))
    blocks: list[str] = []
    withheld = 0
    for turn in outcome.turns:
        revoked = [d for d in turn.cited_document_ids if d not in permitted]
        if revoked:
            withheld += 1
            blocks.append(_render_turn(turn, text=_WITHHELD, budget=len(_WITHHELD)))
            continue
        blocks.append(_render_turn(turn, text=turn.content, budget=budget))

    header = "Earlier in this conversation (already summarised out of your context; oldest first):"
    footer = (
        "\n\nThese are recalled turns, not evidence — they carry no citations. To "
        "quote or cite any document mentioned above, retrieve it now with "
        "search_text or get_document so it is checked against current permissions."
    )
    # Surviving evidence is reported as IDS + CURRENT names, never as recalled
    # text: the ids are what the model re-retrieves through, and the names are
    # the ones permission says are live now — not the ones stored at write time.
    if permitted:
        # Sorted, not dict-ordered: the row order of a permission query is not
        # guaranteed, and an unstable rendering would churn the prompt prefix
        # across turns for no reason (ADR-0016 §2 cache-first prompting).
        names = "\n".join(
            f"- {name} (document_id {doc_id})"
            for doc_id, name in sorted(permitted.items(), key=lambda kv: (kv[1], str(kv[0])))
        )
        footer = f"\n\nDocuments referenced above that you can still access:\n{names}{footer}"

    return ToolHandlerResult(
        content=f"{header}\n\n" + "\n\n".join(blocks) + footer,
        summary=f"{len(outcome.turns)} earlier turn(s)",
        hit_count=len(outcome.turns),
        # Deliberately NO ``passages`` and NO ``document_ids``:
        #  * no passages ⇒ this result never enters the compactor's
        #    ``cited_snippets`` map, so it sits in tier 1 (uncited) and is the
        #    FIRST thing digested under context pressure. Recall is navigation;
        #    evidence must outrank it (#491).
        #  * no document_ids ⇒ recall contributes no citation. There is no
        #    ``conversation`` citation kind and this does not invent one:
        #    ``citations.chunk_id`` is a NOT NULL FK to ``chunks`` and INV-3 says
        #    a citation resolves to a permitted passage the model actually read.
        #    The model re-retrieves to cite; INV-3 stays exactly as strong.
        payload={
            "turns": len(outcome.turns),
            "withheld_turns": withheld,
            "permitted_documents": len(permitted),
            "requested_documents": len(cited_ids),
        },
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name=READ_CONVERSATION_TOOL_NAME,
        description=(
            "Read earlier turns of THIS conversation that have been summarised out "
            "of your context. Use it when the user refers to something you cannot "
            "see — 'as we discussed', 'the number you gave me', 'that second "
            "option' — instead of guessing or asking them to repeat themselves. "
            "Call it with a few distinctive keywords from what you are looking "
            "for, or with no query to read the turns immediately before what you "
            "can already see. It only reaches turns that are NO LONGER in your "
            "context, so it cannot show you the recent messages above. Recalled "
            "turns are conversation, not evidence: they carry no citations, so "
            "re-retrieve any document you want to quote. Do not call this more "
            "than twice in one answer, and never for something already visible."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A few distinctive keywords that would appear in the turn "
                        "you want (all must match). Omit to read the most recent "
                        "turns that have dropped out of context."
                    ),
                },
                "k": {
                    "type": "integer",
                    "description": f"How many turns to return (1-{_MAX_TURNS}).",
                    "minimum": 1,
                    "maximum": _MAX_TURNS,
                },
            },
            # No required args — the no-query read is the common case.
        },
        handler=_read_conversation,
        risk_tier=RiskTier.T0,
        read_only=True,
        # On the ad-hoc default allow-list: reading its own conversation back is
        # not a widened reach (the caller owns every byte of it and can already
        # read it in the UI), it is the repair of a gap the summariser opened.
        default_offered=True,
    ),
)


__all__ = ["READ_CONVERSATION_TOOL_NAME", "TOOLS"]
