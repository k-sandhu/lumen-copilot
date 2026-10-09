"""Recall has no redaction scanner; permitted text is simply bounded."""

import pytest

from app.domain.recall import clip_recall_text


@pytest.mark.parametrize("budget", [0, 1, 10, 120, 600])
@pytest.mark.parametrize(
    "body", ["", "  own words  ", "e" * 10_000, "Project Plan for ORION.pdf" * 100]
)
def test_recall_clip_is_a_literal_bounded_prefix(body: str, budget: int) -> None:
    expected = "" if budget <= 0 else body if len(body) <= budget else body[: budget - 1] + "…"
    assert clip_recall_text(body, budget) == expected
    assert len(clip_recall_text(body, budget)) <= budget
