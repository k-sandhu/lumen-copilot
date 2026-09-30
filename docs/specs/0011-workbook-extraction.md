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
formulas or assert the freshness of a supplied cache. Merged-cell ranges are
reported, and covered cells remain blank: only the source anchor owns a value.
All workbook archives close even on failure. Corrupt bytes raise the existing
typed parser error. Parser dependencies stay lazy and no library is added.

This row projection makes sheet identity, labels, units and positions available
to retrieval without changing exact offsets into the rendered source. Only
future ingestion uses it. Re-ingestion from retained bytes or a new upload is
needed for existing documents; search reindex alone copies existing chunks.
Re-ingestion replaces chunk IDs, so historical citations are not automatically
retargeted and their original offsets must never be rewritten in place.

Acceptance fixtures cover multiple sheets, gaps, blank columns, merged cells,
source units/formats, zero/false and formula cache absence. Negative fixtures
cover corrupt files and a workbook with no nonblank cells.

Deferred: inferred multi-row headers, computed formulas, chart understanding,
bad producer worksheet dimensions and structure-aware chunk boundaries. Long
rows can still span chunks; the canonical-model design addresses that limit.
