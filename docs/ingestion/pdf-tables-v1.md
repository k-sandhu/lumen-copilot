# PDF table candidate — issue #672

Builds on the bounded PDF engine in #671 / PR #724 and ADR-0027. It adds no
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
  Sparse cell metadata, retained model copies and JSON serialization are reserved
  independently of visible text length before rendering/serialization grows.
  Scanned tables stay typed needs-OCR; no image decoder or network is added.
- Generated bordered, borderless, spanned and multi-page gold fixtures must pass
  cell/header association and exact provenance checks. Negative prose-column
  fixtures report document false-positive counts with their denominator.

## Measured generated-fixture results

[pdf-tables-benchmark.json](pdf-tables-benchmark.json) retains 45 document-arm
rows, including incomplete/corrupt documents, parser/build/dependency identities
for successful native results and both current and #625-equivalent pypdf arms.

| Four positive table fixtures | Current pypdf | #625-equivalent pypdf | Rust |
|---|---:|---:|---:|
| Fact coverage | 100% | 100% | 100% |
| Row/value co-presence | 100% | 100% | 100% |
| Explicit cell/header association | 0% | 0% | 100% |
| Page provenance | unavailable | 100% | 100% |
| Exact code-point spans | 100% | 100% | 100% |

Bordered, borderless, spanned and multi-page fixtures each yield one canonical
table. Units, signs and footnote markers survive. Negative prose/unpainted-path
documents produce zero tables: **0/4 document false positives (0%)**. This small
denominator is explicitly a fixture result, not a corpus-wide quality estimate.
The existing layout fixture remains 100% in reading-order agreement.

For the bordered fixture, cold extraction including imports measured 293.8 ms
for current pypdf, 251.4 ms for the provenance projection and 21.0 ms for Rust.
Process peak RSS was 62.54 / 62.54 / 23.28 MiB respectively. Rust's accounted
peak was 182,795 bytes, including serialization reservations. Warm throughput,
scaling and answer-level accuracy are not inferred from these tiny documents.

Verification: 36 Rust workspace tests plus the additional surrounding-column
table-insertion regression passed (37 distinct tests). The sparse-serialization
regression failed under prior accounting and passes with metadata/model/JSON
reservations. Clippy with warnings denied, Rust formatting, Ruff, strict facade
mypy, Windows wheel/import, and 35 targeted Python bridge/runtime/parser/
fidelity tests passed. Test fixtures are source-generated; no third-party PDF,
new dependency or binary is introduced in #672. #671 supplies flate2/sha2 under
MIT OR Apache-2.0 and the #666 runtime supplies the bounded parallel pool.

[~] 2026-10-08: full backend suite deferred because Available MBytes stayed below
4000 during final checks (3429 before the targeted run, 3756 before the final
benchmark); rely on CI. Residual risk: wider backend regressions unverified.

[~] 2026-10-08: local cargo-deny unavailable; inherited CI remains the license/
advisory gate. Residual risk: local transitive advisory scan unverified.

[~] 2026-10-08: held-out corpus, warm/scaling measurements, non-Windows execution
and full #625/#630/#638 stage integration remain unverified. Residual risk:
heuristic detection/continuity and engine subset coverage (#722); text-only,
irregular, rotated/curved ruling geometry and ambiguous continuation may remain
unstructured. OCR, production budgets, evaluation thresholds and cutover belong
to subsequent owner decisions. No live datastores or containers were touched.
Merge gate: hold until measured against the baseline evaluation; a human merges.
