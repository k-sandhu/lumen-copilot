"""Reciprocal-rank fusion of the two retrieval signals — pure, I/O-free (CC-1 #45).

Hybrid retrieval (ADR-0003 §3) runs **two** queries against the permission-filtered
candidate set — pgvector semantic similarity and Postgres full-text (lexical) —
and must combine their rankings into one ordered result. This module is the pure
combiner: given the two ranked lists of chunk ids (best first), it returns a
single fused ranking. No DB, no session, no vendor types — so the ranking policy
is unit-testable with zero mocks and is the same code the live path runs.

**Reciprocal-rank fusion (RRF).** Each list contributes ``1 / (k + rank)`` to a
chunk's score, where ``rank`` is the 0-based position in that list and ``k`` is a
smoothing constant (the standard RRF default, 60). RRF is chosen over a weighted
score blend because the two signals are on **incomparable scales** (cosine
distance vs. ``ts_rank`` lexical score) — fusing by *rank* needs no calibration
and is robust when one signal is missing a document the other found. A chunk that
ranks well in *both* lists rises above one that ranks well in only one, which is
exactly the hybrid intent.

A cross-encoder re-rank over the fused top-N (ADR-0003 §9 / backend/AGENTS.md)
is a documented fast-follow — see :func:`rerank` below (a flagged stub that
preserves the fused order until the OSS reranker lands). It is intentionally
non-blocking for #45.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

# Standard RRF smoothing constant (Cormack et al. 2009). Larger ⇒ flatter
# contribution curve (rank differences matter less); 60 is the well-known default.
_RRF_K = 60


def reciprocal_rank_fusion(
    *ranked_lists: Sequence[UUID],
    k: int = _RRF_K,
) -> list[UUID]:
    """Fuse one or more ranked id lists into a single ranking (best first).

    Args:
        *ranked_lists: each a sequence of chunk ids ordered best→worst (the
            semantic list and the lexical list, but any number is accepted so a
            single-signal call degrades gracefully to that signal's order).
        k: the RRF smoothing constant (> 0). The default is the standard 60.

    Returns:
        The unique chunk ids ordered by descending fused RRF score. An id present
        in several lists accumulates a contribution from each. Ties (equal fused
        score) break deterministically by the id's **first-seen** position across
        the input lists, so the output is stable run-to-run.

    Raises:
        ValueError: if ``k <= 0`` — a non-positive constant makes the early ranks
            explode / divide-by-zero and is a misconfiguration, not a tuning knob.
    """
    if k <= 0:
        raise ValueError("RRF k must be positive")

    scores: dict[UUID, float] = {}
    first_seen: dict[UUID, int] = {}
    order = 0
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
            if chunk_id not in first_seen:
                first_seen[chunk_id] = order
                order += 1

    # Sort by fused score desc, then by first-seen order asc for a stable tiebreak.
    return sorted(scores, key=lambda cid: (-scores[cid], first_seen[cid]))


def rrf_score(rank: int, k: int = _RRF_K) -> float:
    """The RRF contribution of a single 0-based ``rank`` (exposed for callers/tests).

    Lets the service attach a fused score to a returned passage without re-deriving
    the constant, and lets the tests assert the exact contribution. ``rank`` is
    0-based (top hit = rank 0).
    """
    if k <= 0:
        raise ValueError("RRF k must be positive")
    return 1.0 / (k + rank)


def rerank(
    query: str,  # noqa: ARG001 — part of the fast-follow signature, unused in the stub
    chunk_ids: Sequence[UUID],
) -> list[UUID]:
    """Cross-encoder re-rank of the fused candidates — a flagged fast-follow stub.

    ADR-0003 §9 / backend/AGENTS.md call for a cross-encoder (OSS reranker)
    re-rank over the fused top-N before context assembly. Building/serving that
    model is sequenced as a documented fast-follow (it needs a model artifact and
    is explicitly non-blocking for #45). Until it lands this is the identity:
    it returns the fused order unchanged, so the retrieval path is correct (just
    not yet maximally precise) and the seam where the reranker plugs in is named.

    Keeping the signature ``(query, chunk_ids) -> list[UUID]`` means swapping the
    stub for the real reranker is a localized change inside ``retrieval/`` with no
    caller impact.
    """
    return list(chunk_ids)
