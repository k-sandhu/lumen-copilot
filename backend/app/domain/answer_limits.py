"""Stable, content-free disclosures for answer research ceilings (spec 0028)."""

from typing import Literal, cast

AnswerLimitKind = Literal[
    "tool_turns", "tool_calls", "external_calls", "input_tokens", "output_tokens", "cost"
]

LIMIT_MESSAGES: dict[AnswerLimitKind, str] = {
    "tool_turns": "Research stopped at its step limit; this answer uses the sources found so far.",
    "tool_calls": (
        "Research stopped at its lookup limit; this answer uses the sources found so far."
    ),
    "external_calls": (
        "Research stopped at its external lookup limit; "
        "this answer uses the sources found so far."
    ),
    "input_tokens": (
        "Research stopped at its reading limit; this answer uses the sources found so far."
    ),
    "output_tokens": (
        "Research stopped at its response limit; this answer uses the sources found so far."
    ),
    "cost": "Research stopped under its spending limit; this answer uses the sources found so far.",
}


def parse_limit_kind(value: object) -> AnswerLimitKind | None:
    """Read old or unknown stored values without rendering untrusted prose."""
    return (
        cast(AnswerLimitKind, value) if isinstance(value, str) and value in LIMIT_MESSAGES else None
    )


def limit_disclosure(kind: AnswerLimitKind | None) -> dict[str, str] | None:
    """Return the same server-authored object on live and history responses."""
    return {"kind": kind, "message": LIMIT_MESSAGES[kind]} if kind is not None else None
