# Spec 0009 — Boundary-aware chunk overlap

Tracking: [#607](https://github.com/k-sandhu/lumen-copilot/issues/607).

Character chunking retains exactly the configured overlap between consecutive
nonblank windows, including after an early sentence or whitespace cut. Starts
and ends strictly increase. A candidate boundary must leave more than `overlap`
characters in the current window; otherwise use the hard window edge. This
prevents a boundary in already-covered context from producing a duplicate or
non-progressing chunk. The terminal window may be shorter than the target.

Every emitted passage remains an exact slice of the extracted source:
`source[char_start:char_end] == text`. Size is an upper bound, ordinals are
contiguous, blank windows are omitted, and invalid settings raise `ValueError`.
Omitted blank windows may reduce overlap between returned passages; no source
characters with non-whitespace content may be lost.

Acceptance counterexample: 800 `a`, `. `, and 1,500 `b`, at size 1200 and overlap
200, has spans `(0, 801)`, `(601, 1801)`, `(1601, 2302)`. Tests also cover early
whitespace, zero overlap, maximum legal overlap, exact slices and invalid input.

This changes newly ingested chunks only. Re-running ingestion replaces chunk
IDs and offsets; historical citations referencing replaced chunks follow the
existing deletion behavior. Search reindex copies existing chunks and does not
rechunk them. Existing citation offsets are never rewritten in place.
