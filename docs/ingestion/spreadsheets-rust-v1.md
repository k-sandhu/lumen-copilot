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
Generation build identities include format code, shared package/canonical/runtime
code and the pinned dependency graph, so shared repairs invalidate prior output.

The #670 evaluation and #687 promotion remain owner merge gates. Production
budgets, resource measurements and dependency interruption/isolation need
qualification before adoption; accounting is not a process RSS guarantee.

Measured 2026-10-08: all 51 generated Python workbook cases pass with exact
rendered text equality; the additional hidden-row/date/percent/currency fixture
also passes. The paired targeted run passes 103 nodes. Rust tests pass generated
ODS typed/mixed-content/repeat/budget cases, a generated compound BIFF8 XLS and
256 Unicode/native-coordinate properties. Complex Excel display formats remain
unknown; simple percent/currency display annotations are explicitly derived.

Separate dependency #708 / PR #712 repairs large canonical table validation;
its four regression/property tests pass, including a 1,000-cell workbook.
Joint Office checkout: all 32 core Rust tests and Clippy all targets pass offline.
Dependency #713 / PR #714 pins the existing local bridge dependency; complete
cargo-deny checks (advisories, bans, licenses, sources) pass without policy changes.
[~] 2026-10-08: XLS preserves calamine values/formulas/merges, but custom BIFF
formats and legacy hidden-row metadata require further fixtures/qualification.
ODS style/number-format inheritance also needs qualification. Residual risk:
these new-format arms are not full-fidelity replacements and have no Python
baseline to claim a relative win against.
[~] 2026-10-08: XLSX cell values stream through calamine but the metadata scan
currently retains a bounded per-part XML tree. Legacy oversized rectangles
produce an explicit partial outcome before calamine allocation, with no retained
prefix. Dependency operations are not cooperatively cancellable internally.
Residual risk: large-workbook streaming, hard RSS and interruption need measured
qualification/isolation before production use.
[~] 2026-10-08: full backend suite deferred below 4000 MiB available RAM; live
gates/cross-platform wheels/held-out evaluation/RSS/scaling are unverified.
Residual risk: targeted offline fixtures do not prove production capacity or
fidelity. Containers remain untouched.
