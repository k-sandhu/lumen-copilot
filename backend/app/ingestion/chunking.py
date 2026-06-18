"""Overlapping, offset-carrying text chunking — pure, I/O-free (CC-5 #21).

Splits parsed plain text into retrievable passages that **carry exact
``char_start``/``char_end`` offsets into the source text**. Those offsets back
passage-level citations (spec 0004 §4, the contract ``Citation`` schema): a
chunk's text is, by construction, ``source[char_start:char_end]`` — the
round-trip is asserted by the tests, because citations depend on it.

Design choices (kept simple and deterministic for the skeleton):

* **Character windows with overlap.** A fixed ``chunk_size`` window slides by
  ``chunk_size - overlap`` so adjacent chunks share ``overlap`` characters —
  context that would otherwise be split across a boundary is retrievable from
  either side. Size/overlap are configuration (``Settings``), never literals at
  the call site.
* **Boundary-aware, within a bounded look-back.** To avoid cutting mid-sentence
  /mid-word where cheap, the window end is nudged back to the nearest sentence
  or whitespace boundary inside a small look-back band; if none is found the
  hard window edge is used. The nudge only ever moves the cut *earlier*, so
  offsets stay exact and progress is guaranteed (the next window always starts
  after the previous one).
* **Offsets index the ORIGINAL string.** No normalization or trimming changes
  the offsets — leading/trailing whitespace inside a window is preserved so the
  slice is faithful; empty/whitespace-only chunks are dropped (nothing to
  embed or cite), but their would-be span never shifts a neighbour.

This module imports nothing outside the stdlib + typing — it is unit-testable
with zero mocks and is the same code the Celery task runs.
"""

from __future__ import annotations

from dataclasses import dataclass

# How far back from a hard window edge we will look for a nicer cut (sentence or
# whitespace). A fraction of the window: a chunk may shrink to at most this
# fraction below the target to land on a clean boundary, otherwise the hard edge
# is used. Bounded so the search is cheap and a chunk never shrinks below half
# the target. Pure tuning constant, not behaviour.
_BOUNDARY_LOOKBACK_FRACTION = 0.5

# Characters that end a sentence — preferred cut points (we cut just after one).
_SENTENCE_ENDINGS = frozenset({".", "!", "?", "\n"})


@dataclass(frozen=True, slots=True)
class TextChunk:
    """One retrievable passage with its exact span in the source text.

    Invariant (asserted by the tests, relied on by citations): for the parsed
    ``source`` string, ``source[char_start:char_end] == text``. ``ord`` is the
    chunk's zero-based position in document order.
    """

    ord: int
    text: str
    char_start: int
    char_end: int


def _find_boundary(text: str, window_start: int, hard_end: int) -> int:
    """Return a cut position in ``(window_start, hard_end]`` that reads cleanly.

    Searches backwards from ``hard_end`` within a bounded look-back band for, in
    order of preference, the position just after a sentence-ending character,
    then the position after a run of whitespace. Falls back to ``hard_end`` (the
    hard window edge) when neither is found in the band. The returned cut is
    always ``> window_start`` so the window makes progress.
    """
    span = hard_end - window_start
    lookback = int(span * _BOUNDARY_LOOKBACK_FRACTION)
    floor = max(window_start + 1, hard_end - lookback)

    # Prefer a sentence boundary: cut just *after* the ending char.
    for i in range(hard_end - 1, floor - 1, -1):
        if text[i] in _SENTENCE_ENDINGS:
            return i + 1

    # Otherwise a whitespace boundary: cut just *after* the whitespace char.
    for i in range(hard_end - 1, floor - 1, -1):
        if text[i].isspace():
            return i + 1

    # No clean boundary in the band — use the hard window edge.
    return hard_end


def chunk_text(text: str, *, chunk_size: int, overlap: int) -> list[TextChunk]:
    """Split ``text`` into overlapping, offset-carrying :class:`TextChunk`s.

    Args:
        text: the parsed plain text to chunk (offsets index *this* string).
        chunk_size: target maximum window length in characters (> 0).
        overlap: characters adjacent chunks share (0 <= overlap < chunk_size).

    Returns:
        Chunks in document order, each with ``text == text[char_start:char_end]``.
        Empty/whitespace-only windows are skipped; an empty/blank ``text`` yields
        an empty list. ``ord`` is contiguous over the *returned* chunks.

    Raises:
        ValueError: if ``chunk_size <= 0`` or ``overlap`` is out of range — a
            misconfiguration that must fail loudly rather than loop forever.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must satisfy 0 <= overlap < chunk_size")

    total = len(text)
    if total == 0 or not text.strip():
        return []

    chunks: list[TextChunk] = []
    stride = chunk_size - overlap
    window_start = 0
    ordinal = 0

    while window_start < total:
        hard_end = min(window_start + chunk_size, total)
        # Only look for a nicer boundary when we are not already at the very end.
        cut = hard_end if hard_end >= total else _find_boundary(text, window_start, hard_end)

        piece = text[window_start:cut]
        if piece.strip():
            chunks.append(
                TextChunk(
                    ord=ordinal,
                    text=piece,
                    char_start=window_start,
                    char_end=cut,
                )
            )
            ordinal += 1

        if cut >= total:
            break

        # Advance by the stride from the window start, but never past the cut we
        # actually made (a short boundary-trimmed chunk must still make progress
        # and keep ``overlap`` characters of context with its predecessor).
        next_start = min(window_start + stride, cut)
        # Guarantee forward progress even in degenerate overlap/boundary cases.
        if next_start <= window_start:
            next_start = window_start + 1
        window_start = next_start

    return chunks
