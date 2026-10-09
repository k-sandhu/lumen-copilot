# Workbook candidate extraction v1

Tracking: #674. ADR-0026/canonical v1; no production dispatch or cutover change.
Pure bytes + limits returns canonical structure; Python keeps the live path.
XLSX/XLSM values use pinned calamine; ODS uses bounded quick-xml, and legacy XLS
uses calamine after bounded compound-file/BIFF preflight. No macro is executed.

Acceptance follows spec 0011 and #674: named sheets, original A1 positions,
blanks/gaps, zero/false, source formats, formulas with supplied/unavailable cache
and unknown freshness, array/data-table attributes and merge-anchor ownership.
The first nonblank row is heuristic. Merged header associations extend across
their columns as metadata; retrieval text remains the Python baseline projection
so no extra source header or measurement is fabricated. Canonical cells carry
typed cached values, original formats, exact native coordinates and spans.
Date/percent/currency display projections are annotations separate from raw
evidence. Dates use the supplied workbook epoch; formulas are never evaluated.

Policy: include hidden sheets/rows, as the current Python parser does, and label
visibility in diagnostics. Chart parts are placeholders, without generated
facts. Macro-enabled XLSM is read as inert text/values only; unsupported encrypted
workbooks fail closed. ZIP traversal, ratio/expansion/entry/part limits, DTD and
entity expansion, malformed cells/merges and corrupt archives are typed errors.
Row/cell limits return explicit partial outcomes/incomplete regions. Hard memory,
time/work and package security limits return errors, never successful truncation.
Blank sheets retain zero-width native source parts. Renderer v1 joins sheet
blocks with two newlines, matching the Python sheet separator.

The #670 evaluation and #687 promotion remain owner merge gates. Production
budgets, resource measurements and dependency interruption/isolation need
qualification before adoption; accounting is not a process RSS guarantee.
