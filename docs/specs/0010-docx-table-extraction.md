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
Nested tables inside cells are walked in order along with adjacent cell
paragraphs. Each origin's nested content is rendered once: merged aliases retain
the origin's scalar paragraph text and append `[nested tables at R2C1]` before
the existing merge marker. First-row labels also use scalar paragraphs and this
reference, never copies of nested table bodies. References are table-local and
identify retained content, not an additional fact. No generated facts or
units are added. Headers embedded in later rows remain ordinary source text.

Acceptance: table-only text is retained, paragraph/table/paragraph order is
preserved, headers and units remain associated, merged and blank cells retain
positions and identify the shared origin; equal-valued unmerged cells carry no
merge marker. A 1,000-row vertical merge retains its last independent value and
origin without recursive merge lookup, including grid omissions and spans.
Nested merged ancestors do not multiply descendant facts. Nested tables retain
content. Corrupt bytes still raise the
typed `DocumentParseError`; imports remain inside the DOCX helper.

DOCX extraction has hard safety ceilings per document: 2,000,000 emitted
characters (including labels, references and separators), 100,000 work units,
and 32 nested table levels. Work charges every visited block, row, physical
cell, declared grid column, occupied grid position and rendered column,
including omitted columns.
Charges precede grid expansion and output accumulation; scalar joins and header
construction also check the character ceiling before allocating. Exceeding any
ceiling raises `DocumentParseError` with a named extraction limit. No truncated
success is returned: ingestion records the permanent failure before chunking or
embedding. These ceilings bound this traversal, not OOXML archive decompression.

New rendering applies only on ingestion. Existing chunks and citation offsets
are unchanged by deployment or search reindex. Operators may re-run ingestion
from retained original bytes (or upload a new document); this replaces chunks
and their IDs under existing semantics. Historical citations to deleted chunks
are not silently retargeted. Search reindex alone copies the stored old text.

Deferred: revision-mark content, inferred multi-row headers, image/figure
understanding, page-layout reconstruction, and token/structure-aware chunking.
Very wide or long rows may still cross character chunk boundaries; structured
chunking and cell-level provenance require the separately proposed model.
