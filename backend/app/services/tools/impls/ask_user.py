"""The ``ask_user`` clarifying-question tool (spec 0006, #429).

A **control-flow tool, not an executable**: in interactive chat the runtime
intercepts a valid ``ask_user`` call *before* the governed runner — it persists
the question as the turn's assistant message, emits ``event:ask_user``, and ends
the stream with ``done(finishReason=ask_user)``. The user's choice arrives as an
ordinary next message; nothing suspends, nothing waits (the chat WS is
one-directional — spec 0006 §2).

It is still a *registered* tool so the ordinary governance applies to who may
offer it (allow-lists / an assistant's ``tool_allowlist``), it shows up in the
registry surfaces, and its schema+description — the only prompt surface it has —
is versioned here. The handler below runs only when something *other* than the
interactive chat runtime executes it through the runner (a sub-agent, a headless
run — contexts with no user present); it reports a typed ``ok=False`` so a
background run can never block on a question no one will answer (those paths
escalate instead, ADR-0015).
"""

from __future__ import annotations

from typing import Any

from app.domain.chat import (
    ASK_USER_MAX_OPTIONS,
    ASK_USER_MIN_OPTIONS,
    AskUserQuestion,
    AskUserValidationError,
)
from app.domain.tools import ERROR_BAD_ARGS, ERROR_TOOL_ERROR, RiskTier, ToolHandlerResult
from app.services.tools.types import ToolContext, ToolDefinition

ASK_USER_TOOL_NAME = "ask_user"


async def _ask_user(args: dict[str, Any], ctx: ToolContext) -> ToolHandlerResult:
    """Validate-or-refuse (never executes anything).

    In interactive chat the runtime intercepts a VALID call before the runner,
    so reaching this handler means either (a) the arguments don't form a
    renderable question — reject with the reason so the model can fix them (in
    chat this is exactly the #429 AC-N1 recovery path, with a normal
    ``tool_invocations`` + audit trace) — or (b) the context is non-interactive
    (sub-agent / headless run): refuse, there is no user to answer.
    """
    try:
        AskUserQuestion.parse(args)
    except AskUserValidationError as exc:
        return ToolHandlerResult(
            content=(
                f"ask_user rejected: {exc} Fix the arguments and ask again, or "
                "proceed with your best interpretation and state the assumption "
                "you made."
            ),
            ok=False,
            error=ERROR_BAD_ARGS,
            summary="ask_user rejected (bad arguments)",
        )
    return ToolHandlerResult(
        content=(
            "ask_user is unavailable in this context: there is no interactive "
            "user to answer. Proceed with your best interpretation and state "
            "the assumption you made."
        ),
        ok=False,
        error=ERROR_TOOL_ERROR,
        summary="ask_user outside interactive chat",
    )


TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name=ASK_USER_TOOL_NAME,
        description=(
            "Ask the user ONE clarifying question with clickable options, instead "
            "of answering. Use it only when the request is genuinely ambiguous AND "
            "the answer would materially change your response (e.g. which quarter, "
            "which of two projects with the same name). Do not use it for "
            "preferences you can infer, to confirm you understood, or after you "
            "already asked once; never re-ask something the user's message already "
            f"answers. Provide {ASK_USER_MIN_OPTIONS}–"
            f"{ASK_USER_MAX_OPTIONS} short, mutually exclusive options (labels of "
            "1-5 words) that each read as a complete answer. If one option is the "
            "sensible default, put it FIRST and append ' (Recommended)' to its "
            "label. Do NOT add an 'Other'/'Something else' option — the UI always "
            "offers a free-text reply. Calling this ends your turn — the user's "
            "choice arrives as their next message."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The single clarifying question to show the user.",
                },
                "options": {
                    "type": "array",
                    "minItems": ASK_USER_MIN_OPTIONS,
                    "maxItems": ASK_USER_MAX_OPTIONS,
                    "description": (
                        "Mutually exclusive choices. The chosen label is sent back "
                        "verbatim as the user's reply."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "The choice text (a complete answer).",
                            },
                            "description": {
                                "type": "string",
                                "description": "Optional one-line elaboration.",
                            },
                        },
                        "required": ["label"],
                    },
                },
                "allow_free_text": {
                    "type": "boolean",
                    "description": (
                        "Whether a typed custom reply is also a good answer " "(default true)."
                    ),
                },
            },
            "required": ["question", "options"],
        },
        handler=_ask_user,
        risk_tier=RiskTier.T0,
        read_only=True,
        # Part of the ad-hoc default set: interactive chat should always be able
        # to ask. Assistants govern it like any tool via tool_allowlist.
        default_offered=True,
    ),
)


__all__ = ["ASK_USER_TOOL_NAME", "TOOLS"]
