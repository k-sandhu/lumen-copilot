# PDF table candidate — issue #672

Builds on the bounded PDF engine in #671 / PR #724 and ADR-0026. It adds no
production cutover. Canonical table roles and geometry remain heuristic.

Acceptance:

- Painted axis-aligned ruling segments enclose grids. Missing internal segments
  describe spanning origin cells, not duplicated evidence. Cell assignment is
  based on the centre of a supplied glyph box; ambiguous geometry is rejected.
- Borderless tables require at least three consistently aligned rows and two
  columns, short header cells, and a numeric/unit-bearing body column. Plain
  prose columns are retained as text; text-only borderless tables remain unknown.
- Values preserve signs, units and footnote markers. Origin cells include page
  and source-point boxes; empty positions are represented by blank cells.
- A first-row header is heuristic, never a supplied semantic role. Rendering
  labels body values with the appropriate header and gives exact code-point
  evidence spans; derived labels are not assigned independent evidence offsets.
- Adjacent table blocks on consecutive pages join only when columns, header
  text and bottom/top continuation geometry agree, with no intervening source
  block. Repeated header rows remain source evidence. Each origin cell retains
  its native page; source page maps use disjoint exact table-text segments.
  Different schemas, intervening paragraphs and non-continuation positions do
  not join. Unknown continuity stays separate.
- Detected tables replace only their contributing text regions. Other paragraphs,
  headings, sidebars, footnotes and margin furniture remain in source page order.
- Work/memory/output/deadline checks use the same Context as PDF extraction.
  Path depth, Cartesian grid size and assignment work are charged before growth.
  Scanned tables stay typed needs-OCR; no image decoder or network is added.
- Generated bordered, borderless, spanned and multi-page gold fixtures must pass
  cell/header association and exact provenance checks. Negative prose-column
  fixtures report document false-positive counts with their denominator.

[~] 2026-10-08: implementation and benchmark pending this draft. Residual risk:
heuristic detection/continuity needs broader owner-approved evaluation; sparse,
text-only and irregular tables are not assumed to be recoverable.
Merge gate: hold until measured against the baseline evaluation; a human merges.
