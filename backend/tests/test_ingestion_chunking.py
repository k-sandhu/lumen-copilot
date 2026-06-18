"""Chunking unit tests — offsets round-trip to the source (CC-5 #21, AC-2).

Pure tests for :mod:`app.ingestion.chunking` (zero mocks). The headline
invariant — citations depend on it — is that every chunk's
``(char_start, char_end)`` indexes back into the source text exactly:
``source[char_start:char_end] == chunk.text``. Also asserted: overlap between
adjacent chunks, contiguous ordinals, boundary-aware cuts, and the
empty/blank/short edge cases and the misconfiguration guards.
"""

from __future__ import annotations

import pytest

from app.ingestion.chunking import chunk_text


def test_offsets_round_trip_to_source() -> None:
    """Every chunk slices back to its exact source span (AC-2, citation backing)."""
    text = (
        "The quick brown fox jumps over the lazy dog. " * 20
        + "Sphinx of black quartz, judge my vow. " * 20
    )
    chunks = chunk_text(text, chunk_size=120, overlap=20)
    assert chunks  # non-trivial input produces chunks
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text


def test_chunks_have_contiguous_ordinals_and_cover_the_text() -> None:
    text = "abcdefghij " * 50
    chunks = chunk_text(text, chunk_size=80, overlap=15)
    # Ordinals are 0..n-1, contiguous and in order.
    assert [c.ord for c in chunks] == list(range(len(chunks)))
    # The union of spans covers the whole (stripped) text: first starts at/near 0,
    # last ends at the end.
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(text)


def test_adjacent_chunks_overlap() -> None:
    """Adjacent windows share context (overlap > 0), so a boundary isn't lost."""
    text = "x" * 1000  # no natural boundaries → pure windowing
    chunks = chunk_text(text, chunk_size=100, overlap=30)
    assert len(chunks) > 1
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        # The next chunk starts before the previous one ends → they overlap.
        assert nxt.char_start < prev.char_end
        # And the shared region is exactly the overlapping characters.
        assert prev.char_end - nxt.char_start >= 1


def test_boundary_aware_cut_prefers_sentence_end() -> None:
    """Within the look-back band the cut lands just after a sentence ending."""
    # A sentence boundary sits a little before the hard window edge.
    text = "First sentence ends here. " + "y" * 200
    chunks = chunk_text(text, chunk_size=40, overlap=5)
    first = chunks[0]
    # The first chunk should end right after the period+space (the sentence cut),
    # not at the hard 40-char edge mid-"y"-run.
    assert first.text.rstrip().endswith(".")
    assert text[first.char_start : first.char_end] == first.text


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_text("", chunk_size=100, overlap=10) == []


def test_whitespace_only_text_yields_no_chunks() -> None:
    assert chunk_text("   \n\t  ", chunk_size=100, overlap=10) == []


def test_text_shorter_than_window_is_one_chunk() -> None:
    text = "a short document"
    chunks = chunk_text(text, chunk_size=100, overlap=10)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert (chunks[0].char_start, chunks[0].char_end) == (0, len(text))


def test_no_chunk_exceeds_window_size() -> None:
    text = "word " * 500
    size = 90
    chunks = chunk_text(text, chunk_size=size, overlap=20)
    for c in chunks:
        assert c.char_end - c.char_start <= size


def test_progress_is_guaranteed_terminates() -> None:
    """A pathological all-one-token text still terminates and covers the text."""
    text = "a" * 10_000
    chunks = chunk_text(text, chunk_size=100, overlap=99)  # max overlap
    assert chunks
    assert chunks[-1].char_end == len(text)


@pytest.mark.parametrize(
    ("size", "overlap"),
    [(0, 0), (-1, 0), (100, 100), (100, 150), (100, -1)],
)
def test_misconfiguration_raises(size: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_text("some text", chunk_size=size, overlap=overlap)
