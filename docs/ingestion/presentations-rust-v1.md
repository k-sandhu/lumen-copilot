# Presentation candidate extraction v1

Tracking #676; ADR-0026 and canonical schema v1. Pure bytes + limits yields
canonical blocks. PPTX/PPTM and ODP are candidates only; Python remains live.
No macros execute; no URLs, external chart data or image OCR are fetched.

Acceptance follows spec 0012 first: numbered/titled content slides, recursive
groups, full text-frame paragraph/explicit-break retention, tables with blanks
and merge anchor ownership, existing speaker-note body and empty-slide maps.
Canonical renderer v1 uses two-newline block separators; Python's single-line
block separator is projected only in paired parity comparisons.

Reading policy: supplied title placeholders first, then supplied shape positions
top-to-bottom/left-to-right, stable package order on ties or unknown geometry.
Groups retain their hierarchy and source traversal identity. Positions are
explicit source metadata, not an asserted visual-language/layout model.
Chart values come from supplied series caches in chart parts with freshness
unknown. SmartArt keeps supplied diagram text; images retain source alt text in
explicit placeholders. Speaker notes remain labelled and parented to the slide.
No generated figure description becomes source evidence.

Slide/table/cell/shape provenance uses one-based native slide numbers, package
parts and stable source shape identifiers; boxes have explicit point units and
top-left origin when known. Unknown inherited layout geometry stays unknown.
ZIP inflation/entry/part/ratio/traversal limits, corrupt packages/XML and DTD or
entity expansion fail closed with typed errors and the runtime Context deadline.
Oversized media fails the package part budget; no media decoding is attempted.

Generated fixtures, Unicode/provenance properties and paired Python comparison
precede the #670 held-out evaluation and #687 per-format cutover. Production
budgets, process-RSS/scaling measurements and human merge remain separate gates.
