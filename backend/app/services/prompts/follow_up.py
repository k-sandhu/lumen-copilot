"""The follow-up-suggestions prompt (spec 0006, #429).

One cheap post-answer completion that proposes the user's next questions. It
sees ONLY the visible conversation tail (the user's question + the settled
answer) — never retrieved passages or tool output — so it cannot surface
anything the caller wasn't already shown (no new INV-2 surface, spec 0006 §5).
Kept separate from ``GROUNDED_SYSTEM_PROMPT`` so the answer path's prompt (and
its provider cache prefix, ADR-0016 §2) is untouched.
"""

from __future__ import annotations

FOLLOW_UP_PROMPT_VERSION = 1

FOLLOW_UP_SYSTEM_PROMPT = """\
You suggest the user's next questions in an enterprise document-assistant chat.

Given the user's question and the assistant's answer, propose the most useful
follow-up questions the user could ask next. Rules:

- Write each as a short, self-contained question in the user's voice (they will
  be sent verbatim as the user's next message).
- Ground them in this conversation: drill into a detail of the answer, a natural
  next step, or a closely related aspect. Never invent facts, names, or
  documents that were not mentioned.
- No numbering, no commentary, no duplicates of what was already asked.

Respond with ONLY a JSON array of strings, e.g. ["...", "..."].
"""


def render_follow_up_request(*, question: str, answer: str, count: int) -> str:
    """The user-turn body for the suggestions completion.

    ``answer`` is tail-truncated by the caller; ``count`` bounds how many
    suggestions are requested (config, spec 0006 §2).
    """
    return (
        f"The user asked:\n{question}\n\n"
        f"The assistant answered:\n{answer}\n\n"
        f"Suggest up to {count} follow-up questions as a JSON array of strings."
    )


__all__ = [
    "FOLLOW_UP_PROMPT_VERSION",
    "FOLLOW_UP_SYSTEM_PROMPT",
    "render_follow_up_request",
]
