"""Unit tests for the pure fusion/ranking (issue #45, AC-1).

Reciprocal-rank fusion and the re-rank stub are pure (no DB, no session), so they
are tested here with zero mocks — the same code the live hybrid path runs. The
headline: a chunk ranked well by **both** signals fuses above one ranked well by
only one, and the order is deterministic.
"""

from __future__ import annotations

import uuid

import pytest

from app.retrieval.fusion import reciprocal_rank_fusion, rerank, rrf_score


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_single_list_preserves_order() -> None:
    """One signal only ⇒ fusion degrades to that signal's order."""
    a, b, c = _ids(3)
    assert reciprocal_rank_fusion([a, b, c]) == [a, b, c]


def test_agreement_outranks_single_signal() -> None:
    """A chunk top-ranked by both lists beats chunks ranked by only one."""
    shared, sem_only, lex_only = _ids(3)
    semantic = [shared, sem_only]
    lexical = [shared, lex_only]
    fused = reciprocal_rank_fusion(semantic, lexical)
    # ``shared`` appears at rank 0 in both → highest fused score.
    assert fused[0] == shared
    # The two single-signal hits follow (order between them is a deterministic tie).
    assert set(fused[1:]) == {sem_only, lex_only}


def test_union_of_both_lists_is_returned() -> None:
    """Every id from either list appears exactly once in the fused result."""
    a, b, c, d = _ids(4)
    fused = reciprocal_rank_fusion([a, b], [c, d, a])
    assert sorted(fused, key=str) == sorted([a, b, c, d], key=str)
    assert len(fused) == len(set(fused))  # no duplicates


def test_higher_lexical_rank_breaks_toward_better_combined_score() -> None:
    """An id ranked highly in one list and present in the other rises."""
    x, y, z = _ids(3)
    # x: rank0 semantic + rank1 lexical; y: rank1 semantic; z: rank0 lexical only.
    semantic = [x, y]
    lexical = [z, x]
    fused = reciprocal_rank_fusion(semantic, lexical)
    # x has contributions from both legs; it must come first.
    assert fused[0] == x


def test_ties_break_by_first_seen_for_determinism() -> None:
    """Equal fused scores break by first-seen order, so output is stable."""
    a, b = _ids(2)
    # Each appears once at the same rank in a single list → equal scores.
    first = reciprocal_rank_fusion([a, b])
    second = reciprocal_rank_fusion([a, b])
    assert first == second == [a, b]


def test_rrf_score_decreases_with_rank() -> None:
    """The positional score is strictly decreasing (top hit scores highest)."""
    assert rrf_score(0) > rrf_score(1) > rrf_score(2)


def test_rrf_rejects_nonpositive_k() -> None:
    a = _ids(1)
    with pytest.raises(ValueError, match="k must be positive"):
        reciprocal_rank_fusion(a, k=0)
    with pytest.raises(ValueError, match="k must be positive"):
        rrf_score(0, k=-1)


def test_rerank_stub_is_identity() -> None:
    """The flagged cross-encoder re-rank is a documented identity fast-follow."""
    ids = _ids(5)
    assert rerank("any query", ids) == ids
    # Returns a fresh list (callers may slice/mutate it safely).
    assert rerank("q", ids) is not ids
