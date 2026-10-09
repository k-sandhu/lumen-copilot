"""Simple display bounds for permission-filtered conversation recall (#569)."""

from __future__ import annotations

MAX_RECALL_TURN_CHARS = 600


def clip_recall_text(text: str, budget: int) -> str:
    """Return an unchanged prefix, reserving one character for truncation."""
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    return text[: budget - 1] + "…"
