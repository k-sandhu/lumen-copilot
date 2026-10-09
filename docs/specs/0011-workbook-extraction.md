# Spec 0011 — Workbook semantics in extracted text

Tracking: [#614](https://github.com/k-sandhu/lumen-copilot/issues/614).

Render each nonempty sheet with `Sheet: <name>`, followed by source merged-cell
ranges when present, and numbered rows with A1 cell coordinates. Keep all grid
columns, including blanks; do not shift a C-column value into B. Skip completely
blank rows while retaining original row numbers. Zero and false are values.

The first nonblank row supplies header labels as an explicit heuristic. Repeat
those labels beside later values; units present in a header stay attached.
Example: `Row 2 (Sheet Sales): A2 [Region]=West | B2 [Revenue (USD)]=12`.
Non-General number formats are retained as source format annotations, including
percent, currency and date formats. No units or computed values are invented.

Read cached formula values and formula text separately using streaming
workbooks. A formula cell includes its expression and either its cached value
or an explicit `cached value unavailable` marker. Extraction does not evaluate
formulas or assert the freshness of a supplied cache. Availability comes from
the source cell's cache presence and type: a present empty string cache
(`t="str"` with `<v/>`) is supplied, while an absent cache or an empty numeric
placeholder is unavailable. Zero, false, errors and nonempty strings remain
supplied results.

Merged-cell ranges are reported, and covered cells remain blank: only the source
anchor owns a value.
Read and validate merge metadata before choosing nonblank rows or headers.
Suppress any stored value, formula or format at a covered coordinate in both
projections; a row containing only covered values remains blank. Never copy the
anchor into covered cells or promote a hidden covered value to a header.

Ordinary formula strings retain their expression. Array formulas also retain
their source range (`array range=A2:A3`). Data-table formulas retain their source
attributes in the fixed order `ref`, `dt2D`, `dtr`, `r1`, `r2`, `del1`, `del2`,
`ca`, omitting absent attributes. These representations never stringify formula
objects or include process addresses, and repeated extraction of identical
bytes produces identical text.
All workbook archives close even on failure. Corrupt bytes raise the existing
typed parser error. Parser dependencies stay lazy and no library is added.

This row projection makes sheet identity, labels, units and positions available
to retrieval without changing exact offsets into the rendered source. Only
future ingestion uses it. Re-ingestion from retained bytes or a new upload is
needed for existing documents; search reindex alone copies existing chunks.
Re-ingestion replaces chunk IDs, so historical citations are not automatically
retargeted and their original offsets must never be rewritten in place.

Acceptance fixtures cover multiple sheets, gaps, blank columns, merged cells,
source units/formats, zero/false, formula cache absence and cached/uncached array
and data-table formulas with exact repeated extraction equality. Raw XML fixtures
retain numeric and formula values beneath merges, including empty anchors and
covered-only rows, and distinguish empty string caches, numeric placeholders,
absent caches, zero, false, errors and strings across all three formula types.
Negative fixtures cover corrupt files, invalid merged ranges and a workbook with
no nonblank cells.

Deferred: inferred multi-row headers, computed formulas, chart understanding,
bad producer worksheet dimensions and structure-aware chunk boundaries. Long
rows can still span chunks; the canonical-model design addresses that limit.
