# Spec 0010 — DOCX tables in document order

Tracking: [#423](https://github.com/k-sandhu/lumen-copilot/issues/423).

Walk the document's paragraphs and tables in their XML body order. Paragraph
text keeps its existing newline rendering. A table is rendered between
`[Table]` and `[/Table]` markers, one numbered row per line, with stable `C1`,
`C2`, … grid positions. Example:

```text
[Table]
Row 1: C1=Region | C2=Revenue (USD)
Row 2: C1 [Region]=West | C2 [Revenue (USD)]=12
[/Table]
```

The first row supplies column labels as an explicit heuristic, not inferred
semantic types. Later rows repeat these source-derived labels beside values,
including units present in the label. This keeps a retrieved row intelligible
when its header is outside the chunk. Empty cells retain their columns. Merged
origins repeat at the occupied grid positions. Each repeated position appends
`[merged from R2C1]` (with its table-local origin row and column), distinguishing
the merge from independent equal-valued cells. The original position has no
repeat marker. Repeated merged values are context, not multiple measurements.
Nested tables inside cells are
walked recursively along with adjacent cell paragraphs. No generated facts or
units are added. Headers embedded in later rows remain ordinary source text.

Acceptance: table-only text is retained, paragraph/table/paragraph order is
preserved, headers and units remain associated, merged and blank cells retain
positions and identify the shared origin; equal-valued unmerged cells carry no
merge marker. Nested tables retain content. Corrupt bytes still raise the
typed `DocumentParseError`; imports remain inside the DOCX helper.

New rendering applies only on ingestion. Existing chunks and citation offsets
are unchanged by deployment or search reindex. Operators may re-run ingestion
from retained original bytes (or upload a new document); this replaces chunks
and their IDs under existing semantics. Historical citations to deleted chunks
are not silently retargeted. Search reindex alone copies the stored old text.

Deferred: revision-mark content, inferred multi-row headers, image/figure
understanding, page-layout reconstruction, and token/structure-aware chunking.
Very wide or long rows may still cross character chunk boundaries; structured
chunking and cell-level provenance require the separately proposed model.
