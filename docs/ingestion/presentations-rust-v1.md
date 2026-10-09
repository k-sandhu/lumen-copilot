# Presentation candidate extraction v1

Tracking #676; ADR-0027 and canonical schema v1. Pure bytes + limits yields
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
Generation build identities include format code, shared package/canonical/runtime
code and the pinned dependency graph, so shared repairs invalidate prior output.

Generated fixtures, Unicode/provenance properties and paired Python comparison
precede the #670 held-out evaluation and #687 per-format cutover. Production
budgets, process-RSS/scaling measurements and human merge remain separate gates.

Verified 2026-10-08: all 20 file-based Python slide fixtures pass after projecting
canonical block separators; the combined candidate/baseline run passes 41 nodes.
The baseline's in-memory library monkeypatch test is not a file parity case;
the candidate never creates notes or modifies package bytes. Five Rust tests
pass, including 256 Unicode/shape/source-part properties, cache/diagram/image
fixtures, explicit point boxes and position order, ODP mixed text and blank maps,
typed package/media/DTD/cancellation negatives. Clippy all targets, touched Ruff,
formatting and offline cargo-deny licenses/advisories pass.

Separate dependency #708 / PR #712 repairs large canonical table validation;
its four regression/property tests pass alongside this format's tests.
Joint Office checkout: all 32 core Rust tests and Clippy all targets pass offline.
Dependency #713 / PR #714 pins the existing local bridge dependency; complete
cargo-deny checks (advisories, bans, licenses, sources) pass without policy changes.
[~] 2026-10-08: inherited layout transforms, rotated/visual-language reading
order, full ODP style/chart inheritance and uncached external chart values need
qualification. Residual risk: these candidate features are not a full visual
layout model; no external values or OCR are invented. Source group order and
unknown geometry remain explicit. Known positions precede unknown positions;
ties retain source order, and speaker notes follow slide shapes.
[~] 2026-10-08: full backend suite deferred below 4000 MiB available RAM; live
gates/cross-platform wheels/held-out fidelity/RSS/throughput/scaling unverified.
Residual risk: generated fixtures do not establish production fidelity or
capacity. Containers untouched. XML is read via bounded events and retained in
per-part trees; accounting/dependency cancellation is not a hard RSS guarantee.
