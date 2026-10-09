# HTML/XHTML/MHTML candidate (#679)

HTML recovery is offline and inert. A bounded lexical preflight reserves DOM
nodes before parser allocation, then a depth/work-bounded walk extracts supplied
headings/lists/preformatted text and table cells/spans. Main/article elements take
precedence over body. Navigation/footer/cookie heuristics are reported; tables
within selected main content are protected. Link targets are inert annotations,
never fetch instructions. Title and supplied metadata dates remain metadata.

MHTML walks bounded MIME parts, decodes transfer encoding, and selects the root
HTML part using the supplied start identifier (otherwise the first HTML part).
Resources are not fetched, executed or embedded. Charset labels and decoding
errors are reported. NATIVE_HTML_ENABLED gates upload/cutover; NATIVE_HTML_SHADOW
does not admit new types. Both default false. Python remains unchanged by default.

Acceptance: main tables survive boilerplate removal, roles/links/metadata retained,
MHTML root decoding, exact Unicode offsets and element paths, scripts inert,
deep/large markup fails with typed budgets, opt-in routing.

Merge gate: hold until measured against the baseline evaluation; a human merges.

Dependencies: html5ever 0.35.0 and markup5ever_rcdom 0.35.0+unofficial
(MIT OR Apache-2.0), mailparse 0.16.1 (0BSD), sha2 0.11.0 (MIT OR Apache-2.0).
0BSD is reviewed as a permissive grant without attribution/copyleft obligations;
the policy adds only that SPDX identifier. CSS selectors and their copyleft or
unmaintained dependencies are absent. All four cargo-deny gates pass.

## Verification (2026-10-09)

Four Rust tests including Unicode/element-path and cell-location properties pass,
as do clippy, fmt and all cargo-deny gates. Installed wheel: 19 targeted Python
checks pass; a partial-live-success regression was red before its fix. Ruff and
strict backend-configured mypy pass (9 changed files). Generated HTML/XHTML/MHTML
all score 100% facts, association, order and exact offsets; Python is unsupported.
[Measured rows](reports/html.json). These tiny cold-import arms establish no speed target.

- [~] 2026-10-09: full backend suite deferred at 3156 MiB (<4000); residual risk:
  unrelated regressions await CI.
- [~] 2026-10-09: recognition budgets (#728), held-out evaluation, hard RSS and
  canonical pipeline persistence (#667–#669) remain gated; residual risk: candidate
  accounting alone cannot qualify production recognition or provenance retention.
- [s] 2026-10-09: live checks excluded by task; residual risk: no deployed round-trip.

[x] 2026-10-09: binary HTML/MHTML-root regression failed before correction;
6 Rust HTML tests and 3 installed HTML Python tests passed after. Clippy/all-targets
passed. Binary signatures are rejected before explicit charset decoding too.
